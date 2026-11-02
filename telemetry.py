"""OpenTelemetry wiring, replacing the previous aioprometheus pull-based setup.

Everything is configured through the standard OTEL_* environment variables, the
same convention the other services in the cluster use:

    OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.observability.svc.cluster.local:4318
    OTEL_SERVICE_NAME=tg-yt-dlp

When OTEL_EXPORTER_OTLP_ENDPOINT is unset (local runs, tests) no exporter is
installed and every instrument below stays a cheap no-op, so nothing has to be
guarded at the call site.
"""

import logging
import os
from collections.abc import Callable

from opentelemetry import metrics, trace

LOG = logging.getLogger(__name__)

# Set by the image build (see Dockerfile ARG BUILD_TIMESTAMP). Exported as a
# gauge so a stale image can be alerted on -- the failure mode that let this
# bot run a 14-month-old yt-dlp unnoticed.
BUILD_TIMESTAMP = float(os.environ.get("BUILD_TIMESTAMP") or 0)

_provider_installed = False
_logger_provider = None


def setup() -> None:
    """Install the OTLP exporters. Safe to call once, early in startup."""
    global _provider_installed, _logger_provider
    if _provider_installed:
        return
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        LOG.info("OTEL_EXPORTER_OTLP_ENDPOINT unset, telemetry disabled")
        return

    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    import yt_dlp.version

    # Resource.create() already picks up OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES;
    # the yt-dlp version is the one attribute worth carrying that it can't know.
    resource = Resource.create({"yt_dlp.version": yt_dlp.version.__version__})

    trace.set_tracer_provider(TracerProvider(resource=resource))
    trace.get_tracer_provider().add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    metrics.set_meter_provider(
        MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
        )
    )

    # Logs also go out over OTLP, with severity, service.name, the trace_id of
    # the current span, and a traceback as one record. The stdout handler stays
    # for kubectl logs.
    _logger_provider = LoggerProvider(resource=resource)
    _logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(_logger_provider)
    logging.getLogger().addHandler(LoggingHandler(logger_provider=_logger_provider))

    _provider_installed = True
    LOG.info("telemetry exporting to %s (yt-dlp %s)", endpoint, yt_dlp.version.__version__)


def shutdown() -> None:
    """Flush pending spans/metrics on the way out."""
    if not _provider_installed:
        return
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider(), _logger_provider):
        if hasattr(provider, "shutdown"):
            try:
                provider.shutdown()
            except Exception:
                LOG.warning("telemetry shutdown failed", exc_info=True)


# Instruments are created against the proxy provider, so defining them at import
# time and calling setup() afterwards works -- the proxy resolves once the real
# provider is installed.
_meter = metrics.get_meter("tg-yt-dlp")
tracer = trace.get_tracer("tg-yt-dlp")

downloads = _meter.create_counter(
    "tg_yt_dlp.downloads",
    description="Download attempts, by mode/outcome/domain, and error.type for errors",
)
download_duration = _meter.create_histogram(
    "tg_yt_dlp.download.duration",
    unit="s",
    description="Time from a free download slot to the end of the request, by mode/outcome",
)
slot_wait = _meter.create_histogram(
    "tg_yt_dlp.download.slot_wait",
    unit="s",
    description="Time that a request waited for a free download slot, by mode",
)
download_size = _meter.create_histogram(
    "tg_yt_dlp.download.size",
    unit="By",
    description="Size of the downloaded file before the size check, by mode",
)
in_flight = _meter.create_up_down_counter(
    "tg_yt_dlp.downloads.in_flight",
    description="Downloads currently running",
)
waiting = _meter.create_up_down_counter(
    "tg_yt_dlp.downloads.waiting",
    description="Requests that wait for a free download slot",
)


def observe_poll_age(age: Callable[[], float]) -> None:
    """Export the seconds since Telegram last answered getUpdates."""

    def callback(_options):
        yield metrics.Observation(age(), {})

    _meter.create_observable_gauge(
        "tg_yt_dlp.poll.age",
        unit="s",
        description="Seconds since Telegram last answered getUpdates",
        callbacks=[callback],
    )


def _observe_build_timestamp(_options):
    yield metrics.Observation(BUILD_TIMESTAMP, {})


if BUILD_TIMESTAMP:
    _meter.create_observable_gauge(
        "tg_yt_dlp.build.timestamp",
        unit="s",
        description="Unix time the running image was built",
        callbacks=[_observe_build_timestamp],
    )


# The old link_domain counter used the raw netloc as a label, which is
# attacker-controlled on a public bot and unbounded. Bucketing to a known set
# keeps the useful signal without letting anyone blow up cardinality.
_KNOWN_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "facebook.com",
    "reddit.com",
    "twitch.tv",
    "vimeo.com",
    "soundcloud.com",
    "vk.com",
    "t.me",
)


def bucket_domain(netloc: str) -> str:
    host = netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    for known in _KNOWN_DOMAINS:
        if host == known or host.endswith("." + known):
            return known
    return "other"
