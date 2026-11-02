import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from telegram.error import BadRequest

import main
import telemetry
import yt_dlp
from conftest import needs_ffmpeg


@pytest.mark.parametrize(
    "text, url",
    [
        ("https://youtu.be/abc", "https://youtu.be/abc"),
        ("look at this https://x.com/a/status/1?s=20 lol", "https://x.com/a/status/1?s=20"),
        ('"https://vimeo.com/1"', "https://vimeo.com/1"),
        ("no link here", None),
    ],
)
def test_url_extraction(text, url):
    match = main.URL_RE.search(text)
    assert (match.group(0) if match else None) == url


@pytest.mark.parametrize(
    "netloc, bucket",
    [
        ("www.youtube.com", "youtube.com"),
        ("m.youtube.com:443", "youtube.com"),
        ("YOUTU.BE", "youtu.be"),
        ("notyoutube.com", "other"),
        ("evil.example", "other"),
    ],
)
def test_bucket_domain(netloc, bucket):
    assert telemetry.bucket_domain(netloc) == bucket


def test_downloaded_file_is_the_largest_complete_file(tmp_path):
    (tmp_path / "small.webm").write_bytes(b"x" * 10)
    (tmp_path / "large.mp4").write_bytes(b"x" * 100)
    (tmp_path / "larger.mp4.part").write_bytes(b"x" * 1000)
    (tmp_path / "subdir").mkdir()
    assert main._downloaded_file(str(tmp_path)) == str(tmp_path / "large.mp4")


def test_downloaded_file_when_empty(tmp_path):
    assert main._downloaded_file(str(tmp_path)) is None


def test_file_id_cache_removes_the_least_recently_used_item():
    cache = main.FileIdCache(2)
    cache.put(("video", "a"), "A")
    cache.put(("video", "b"), "B")
    assert cache.get(("video", "a")) == "A"  # Now "b" is the least recently used.
    cache.put(("video", "c"), "C")
    assert cache.get(("video", "b")) is None
    assert cache.get(("video", "a")) == "A"
    assert cache.get(("video", "c")) == "C"
    cache.drop(("video", "a"))
    assert cache.get(("video", "a")) is None


def test_sent_file_id():
    media = SimpleNamespace(file_id="F")
    empty = dict(video=None, audio=None, animation=None, document=None)
    assert main._sent_file_id(SimpleNamespace(**{**empty, "video": media})) == "F"
    assert main._sent_file_id(SimpleNamespace(**{**empty, "animation": media})) == "F"
    assert main._sent_file_id(SimpleNamespace(**empty)) is None


async def _get(app, path):
    async with TestClient(TestServer(app)) as client:
        response = await client.get(path)
        return response.status, await response.json()


def test_health_when_polling_works():
    tracker = SimpleNamespace(last_ok=time.monotonic())
    status, body = asyncio.run(_get(main._health_app(tracker), "/health"))
    assert status == 200
    assert body["status"] == "ok"


def test_health_when_polling_stopped():
    old = time.monotonic() - main.MAX_POLL_AGE.total_seconds() - 1
    tracker = SimpleNamespace(last_ok=old)
    status, body = asyncio.run(_get(main._health_app(tracker), "/health"))
    assert status == 503
    assert body["status"] == "stale"


@needs_ffmpeg
def test_media_meta_for_video(sample_video, tmp_path):
    meta = main._media_meta(str(sample_video), "video", {}, str(tmp_path))
    assert meta["duration"] == 3
    assert (meta["width"], meta["height"]) == (320, 240)
    thumbnail = meta["thumbnail"]
    assert thumbnail.is_file()
    assert thumbnail.stat().st_size <= main.THUMBNAIL_MAX_BYTES


@needs_ffmpeg
def test_media_meta_for_audio(sample_video, tmp_path):
    info = {"title": "Song", "uploader": "Channel", "artist": "Artist"}
    meta = main._media_meta(str(sample_video), "audio", info, str(tmp_path))
    assert meta == {"duration": 3, "title": "Song", "performer": "Artist"}


def test_media_meta_when_ffprobe_fails(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    assert main._media_meta(str(broken), "video", {}, str(tmp_path)) == {}


class FakeProgress:
    async def edit_text(self, text):
        pass

    async def delete(self):
        pass


class FakeMessage:
    message_id = 1

    def __init__(self, refuse_file_ids=False):
        self.sent = []
        self.refuse_file_ids = refuse_file_ids

    async def reply_video(self, video, **kwargs):
        if isinstance(video, str) and self.refuse_file_ids:
            raise BadRequest("Wrong file identifier")
        self.sent.append((video, kwargs))
        media = SimpleNamespace(file_id=f"file-id-{len(self.sent)}")
        return SimpleNamespace(video=media, audio=None, animation=None, document=None)


class FakeSpan:
    def set_attribute(self, key, value):
        pass


def _run(message, url, workdir):
    return asyncio.run(
        main._download_and_send(
            message, FakeProgress(), url, main.VIDEO_FORMAT, "video", str(workdir), FakeSpan()
        )
    )


@pytest.fixture
def count_extractions(monkeypatch):
    calls = []
    original = main.yt_dlp.YoutubeDL.extract_info

    def counted(self, *args, **kwargs):
        calls.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(main.yt_dlp.YoutubeDL, "extract_info", counted)
    monkeypatch.setattr(main, "_file_ids", main.FileIdCache(10))
    return calls


@needs_ffmpeg
def test_download_extracts_once_and_sends_metadata(sample_url, tmp_path, count_extractions):
    message = FakeMessage()
    assert _run(message, sample_url, tmp_path) == "ok"
    assert len(count_extractions) == 1

    [(video, kwargs)] = message.sent
    assert isinstance(video, Path) and video.suffix == ".mp4"
    assert kwargs["supports_streaming"] is True
    assert (kwargs["width"], kwargs["height"], kwargs["duration"]) == (320, 240, 3)
    assert isinstance(kwargs["thumbnail"], Path)


@needs_ffmpeg
def test_second_request_uses_the_cached_file_id(sample_url, tmp_path, count_extractions):
    message = FakeMessage()
    _run(message, sample_url, tmp_path / "first")
    assert _run(message, sample_url, tmp_path / "second") == "cached"
    assert len(count_extractions) == 1
    assert message.sent[-1][0] == "file-id-1"


@needs_ffmpeg
def test_refused_file_id_downloads_again(sample_url, tmp_path, count_extractions):
    main._file_ids.put(("video", sample_url), "expired-file-id")
    message = FakeMessage(refuse_file_ids=True)
    assert _run(message, sample_url, tmp_path) == "ok"
    assert len(count_extractions) == 1
    assert main._file_ids.get(("video", sample_url)) == "file-id-1"


@pytest.mark.parametrize(
    "message, label",
    [
        ("ERROR: [Instagram] x: No video formats found!", "format"),
        ("ERROR: [youtube] x: Requested format is not available", "format"),
        ("ERROR: [Instagram] x: Instagram sent an empty media response. Use --cookies", "login_required"),
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "login_required"),
        ("ERROR: [youtube] x: Video unavailable", "unavailable"),
        ("ERROR: unable to download webpage: HTTP Error 429: Too Many Requests", "rate_limited"),
        ("ERROR: Unsupported URL: https://example.com", "unsupported"),
        ("ERROR: something new", "download_other"),
    ],
)
def test_classify_download_errors(message, label):
    assert main.classify_error(yt_dlp.utils.DownloadError(message)) == label


def test_classify_other_errors():
    assert main.classify_error(BadRequest("x")) == "telegram"
    assert main.classify_error(RuntimeError("x")) == "other"
