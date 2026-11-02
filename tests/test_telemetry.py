"""handle_url end to end, with in-memory exporters for spans and metrics."""

import asyncio
from types import SimpleNamespace

import pytest
import yt_dlp
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import main
from conftest import needs_ffmpeg
from test_main import FakeMessage, FakeProgress

# The global providers can be set one time per process. The instruments in
# telemetry.py were made on the proxy providers, so they use these.
_spans = InMemorySpanExporter()
_reader = InMemoryMetricReader()
_tracer_provider = TracerProvider()
_tracer_provider.add_span_processor(SimpleSpanProcessor(_spans))
trace.set_tracer_provider(_tracer_provider)
metrics.set_meter_provider(MeterProvider(metric_readers=[_reader]))


class FakeUpdateMessage(FakeMessage):
    def __init__(self, text, chat_type="private"):
        super().__init__()
        self.text = text
        self.caption = None
        self.entities = self.caption_entities = ()
        self.chat = SimpleNamespace(type=chat_type)
        self.texts = []

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)
        return FakeProgress()


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    _spans.clear()
    monkeypatch.setattr(main, "_file_ids", main.FileIdCache(10))
    monkeypatch.setattr(main, "TMP_DIR", str(tmp_path))


def _handle(text, chat_type="private"):
    message = FakeUpdateMessage(text, chat_type)
    asyncio.run(main.handle_url(SimpleNamespace(effective_message=message), None))
    return message


def _points(name):
    data = _reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


@needs_ffmpeg
def test_successful_download(sample_url):
    _handle(f"look {sample_url}")

    spans = {span.name: span for span in _spans.get_finished_spans()}
    root = spans["handle_url"]
    assert root.attributes["download.outcome"] == "ok"
    for name in ("wait_slot", "probe", "download", "media_meta", "upload"):
        assert spans[name].parent.span_id == root.context.span_id, name

    for name in ("tg_yt_dlp.downloads", "tg_yt_dlp.download.duration"):
        [point] = [p for p in _points(name) if p.attributes["outcome"] == "ok"]
        assert "error.type" not in point.attributes
    [size] = _points("tg_yt_dlp.download.size")
    assert size.sum > 1000
    assert _points("tg_yt_dlp.download.slot_wait")


def test_download_error(monkeypatch):
    def fail(url, fmt):
        raise yt_dlp.utils.DownloadError("ERROR: [Instagram] x: No video formats found!")

    monkeypatch.setattr(main, "_probe", fail)
    _handle("https://www.instagram.com/reel/x/")

    root = next(s for s in _spans.get_finished_spans() if s.name == "handle_url")
    assert root.attributes["error.type"] == "format"
    assert root.status.status_code == trace.StatusCode.ERROR

    # The reader is cumulative, so select the points of this test.
    [count] = [p for p in _points("tg_yt_dlp.downloads") if p.attributes["outcome"] == "error"]
    assert dict(count.attributes) == {
        "mode": "video",
        "domain": "instagram.com",
        "outcome": "error",
        "error.type": "format",
        "chat.type": "private",
    }
    [duration] = [
        p for p in _points("tg_yt_dlp.download.duration") if p.attributes["outcome"] == "error"
    ]
    assert duration.attributes["error.type"] == "format"


@needs_ffmpeg
def test_silent_mode_says_only_uploading(sample_url):
    message = _handle(f"look {sample_url}", chat_type="channel")
    assert message.texts == ["⬆️ Uploading..."]
    assert len(message.sent) == 1


def test_silent_mode_says_nothing_on_error(monkeypatch):
    def fail(url, fmt):
        raise yt_dlp.utils.DownloadError("ERROR: [twitter] 1: No video could be found in this tweet")

    monkeypatch.setattr(main, "_probe", fail)
    message = _handle("https://x.com/a/status/1", chat_type="supergroup")
    assert message.texts == [] and message.sent == []
    root = next(s for s in _spans.get_finished_spans() if s.name == "handle_url")
    assert root.attributes["error.type"] == "no_media"
    assert root.attributes["chat.type"] == "supergroup"


def test_silent_mode_says_nothing_when_too_large(monkeypatch):
    monkeypatch.setattr(main, "_probe", lambda url, fmt: {"filesize": main.MAX_TG_FILE_SIZE + 1})
    message = _handle("https://x.com/a/status/1", chat_type="channel")
    assert message.texts == [] and message.sent == []
