import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import OrderedDict
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiohttp import web
from telegram import MessageEntity, Update
from telegram.constants import ChatType
from opentelemetry.trace import Status, StatusCode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import telemetry

LOG = logging.getLogger("tg-yt-dlp")

URL_RE = re.compile(r"https?://[^\s<>\"']+")

# A local Bot API server (https://github.com/tdlib/telegram-bot-api) started
# with --local accepts uploads up to 2000 MB. The cloud API accepts 50 MB.
TELEGRAM_API_URL = os.environ.get("TELEGRAM_API_URL", "").rstrip("/")
# Send the file path to the local server and do not upload the file. The server
# must see TMP_DIR at the same path, for example through a shared volume.
TELEGRAM_LOCAL_MODE = os.environ.get("TELEGRAM_LOCAL_MODE", "").lower() in ("1", "true", "yes")
_DEFAULT_MAX_SIZE = (2000 if TELEGRAM_API_URL else 50) * 1024 * 1024
MAX_TG_FILE_SIZE = int(os.environ.get("MAX_TG_FILE_SIZE", _DEFAULT_MAX_SIZE))
UPLOAD_TIMEOUT = float(os.environ.get("UPLOAD_TIMEOUT_SECONDS", 600))
TMP_DIR = os.environ.get("TMP_DIR") or os.path.join(os.path.dirname(__file__), "tmp")
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", 9100))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 2))
# /health returns 503 when Telegram did not answer a getUpdates call for this
# time. Long polling returns at least every 10 seconds when the bot works.
MAX_POLL_AGE = timedelta(seconds=int(os.environ.get("MAX_POLL_AGE_SECONDS", 120)))
FILE_ID_CACHE_SIZE = int(os.environ.get("FILE_ID_CACHE_SIZE", 1000))

# Orphans only: the happy path deletes its own workdir. This is the safety net
# for crashes, and the TTL is generous enough never to race a live download.
ORPHAN_TTL = timedelta(minutes=int(os.environ.get("ORPHAN_TTL_MINUTES", 60)))
JANITOR_INTERVAL = timedelta(minutes=10)

VIDEO_FORMAT = "bestvideo[ext=mp4][vcodec=avc1.4D401F]+bestaudio[ext=m4a]/best"
AUDIO_FORMAT = "bestaudio/best"

HELP_TEXT = (
    "YOU SEND LINK, ME SENDS VIDEO. YOU SEND /audio LINK, ME SENDS AUDIO. "
    f"TELEGRAM LIMIT {MAX_TG_FILE_SIZE // 1024 // 1024}MB, C'EST LA VIE"
)

# Telegram rejects thumbnails that are larger than 320 px or 200 kB.
THUMBNAIL_MAX_PX = 320
THUMBNAIL_MAX_BYTES = 200 * 1024

# yt-dlp is blocking and CPU/IO heavy; bound it so one busy chat can't spawn an
# unbounded number of ffmpeg processes. The semaphore bounds queueing, the pool
# bounds the threads themselves.
_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS, thread_name_prefix="ytdlp")
_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


class PollTracker(HTTPXRequest):
    """The request object for getUpdates only. It records when Telegram last answered."""

    def __init__(self) -> None:
        super().__init__(connection_pool_size=1)
        self.last_ok = time.monotonic()

    async def do_request(self, *args, **kwargs) -> tuple[int, bytes]:
        code, payload = await super().do_request(*args, **kwargs)
        if code == 200:
            self.last_ok = time.monotonic()
        return code, payload


class FileIdCache:
    """In-memory LRU map from a request key to the file_id of a file the bot sent.

    Telegram keeps the files that a bot sends, so the bot can send the same file
    again by its file_id, without a new download or upload.
    """

    def __init__(self, size: int) -> None:
        self._size = size
        self._items: OrderedDict[tuple, str] = OrderedDict()

    def get(self, key: tuple) -> str | None:
        file_id = self._items.get(key)
        if file_id is not None:
            self._items.move_to_end(key)
        return file_id

    def put(self, key: tuple, file_id: str) -> None:
        self._items[key] = file_id
        self._items.move_to_end(key)
        while len(self._items) > self._size:
            self._items.popitem(last=False)

    def drop(self, key: tuple) -> None:
        self._items.pop(key, None)


_poll_tracker = PollTracker()
_file_ids = FileIdCache(FILE_ID_CACHE_SIZE)


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs each request URL at INFO, and the Bot API URL contains the bot
    # token. It also adds a line every 10 seconds for getUpdates.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    telemetry.setup()
    telemetry.observe_poll_age(lambda: time.monotonic() - _poll_tracker.last_ok)
    os.makedirs(TMP_DIR, exist_ok=True)

    builder = (
        ApplicationBuilder()
        .token(os.environ["TELEGRAM_TOKEN"])
        .get_updates_request(_poll_tracker)
        .media_write_timeout(UPLOAD_TIMEOUT)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
    )
    if TELEGRAM_API_URL:
        builder = (
            builder.base_url(f"{TELEGRAM_API_URL}/bot")
            .base_file_url(f"{TELEGRAM_API_URL}/file/bot")
            .local_mode(TELEGRAM_LOCAL_MODE)
        )
    app = builder.build()
    for handler in _handlers():
        app.add_handler(handler)
    app.add_error_handler(handle_error)

    # run_polling installs its own SIGINT/SIGTERM handlers and drives shutdown
    # through post_shutdown, so there is nothing to unwind here.
    # Explicit: without it, Telegram uses the list from the previous call.
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CHANNEL_POST])


def _handlers() -> list:
    private = filters.ChatType.PRIVATE
    # New messages and channel posts only: an edit must not start a second download.
    new = filters.UpdateType.MESSAGE | filters.UpdateType.CHANNEL_POST
    # In channels and groups the bot is silent, and most posts have no link, so
    # only posts with a link (also in a caption, also a hidden link) get through.
    has_link = (
        filters.Entity(MessageEntity.URL)
        | filters.Entity(MessageEntity.TEXT_LINK)
        | filters.CaptionEntity(MessageEntity.URL)
        | filters.CaptionEntity(MessageEntity.TEXT_LINK)
    )
    return [
        CommandHandler("help", handle_help, filters=private),
        CommandHandler("start", handle_help, filters=private),
        CommandHandler("audio", handle_url, filters=new),
        MessageHandler(private & new & filters.TEXT & ~filters.COMMAND, handle_url),
        MessageHandler(~private & new & has_link & ~filters.COMMAND, handle_url),
    ]


class _NoProgress:
    """The progress message in silent mode: it shows nothing."""

    async def edit_text(self, text):
        pass

    async def delete(self):
        pass


def _find_url(message, text: str) -> str | None:
    # Match the URL out of the message rather than assuming the whole message is
    # one, so "look at this <url>" works.
    match = URL_RE.search(text)
    if match:
        return match.group(0)
    # A hidden link: the text shows a word, and the URL is only in the entity.
    for entity in (message.entities or ()) + (message.caption_entities or ()):
        if entity.type == MessageEntity.TEXT_LINK and entity.url:
            return entity.url
    return None


async def _post_init(app) -> None:
    app.bot_data["janitor"] = asyncio.create_task(clean_orphans())
    runner = web.AppRunner(_health_app(_poll_tracker))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HEALTH_PORT).start()
    app.bot_data["health_runner"] = runner
    LOG.info(
        "health server on :%s, %s download slots, %sMB limit, api %s",
        HEALTH_PORT,
        MAX_CONCURRENT_DOWNLOADS,
        MAX_TG_FILE_SIZE // 1024 // 1024,
        TELEGRAM_API_URL or "cloud",
    )


async def _post_shutdown(app) -> None:
    janitor = app.bot_data.get("janitor")
    if janitor:
        janitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await janitor
    runner = app.bot_data.get("health_runner")
    if runner:
        await runner.cleanup()
    _pool.shutdown(wait=False, cancel_futures=True)
    telemetry.shutdown()


def _health_app(poll_tracker: PollTracker) -> web.Application:
    async def health(_request):
        age = time.monotonic() - poll_tracker.last_ok
        stale = age > MAX_POLL_AGE.total_seconds()
        body = {
            "status": "stale" if stale else "ok",
            "yt_dlp": yt_dlp.version.__version__,
            "last_poll_seconds_ago": round(age, 1),
        }
        return web.json_response(body, status=503 if stale else 200)

    app = web.Application()
    app.router.add_get("/health", health)
    return app


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    text = message.text or message.caption or ""
    # In channels and groups the bot says nothing. It only replies with the
    # file, and with "Uploading..." after a successful download.
    silent = message.chat.type != ChatType.PRIVATE
    chat_type = message.chat.type

    if text.startswith("/audio"):
        mode, fmt = "audio", AUDIO_FORMAT
        text = text[len("/audio"):]
    else:
        mode, fmt = "video", VIDEO_FORMAT

    url = _find_url(message, text)
    if url is None:
        telemetry.downloads.add(
            1, {"mode": mode, "outcome": "invalid_url", "chat.type": chat_type}
        )
        if not silent:
            await message.reply_text(
                "Invalid URL. Please send a valid video URL",
                reply_to_message_id=message.message_id,
            )
        return

    domain = telemetry.bucket_domain(urlparse(url).netloc)
    attrs = {"mode": mode, "domain": domain, "chat.type": chat_type}

    if silent:
        progress = _NoProgress()
    else:
        progress = await message.reply_text(
            "⬇️ Downloading...", reply_to_message_id=message.message_id
        )
    workdir = tempfile.mkdtemp(dir=TMP_DIR)
    outcome, error_type, started = "error", None, None

    with telemetry.tracer.start_as_current_span("handle_url") as span:
        span.set_attribute("download.mode", mode)
        span.set_attribute("url.domain", domain)
        span.set_attribute("chat.type", chat_type)
        try:
            # The duration starts when a slot is free. The wait is a separate metric.
            telemetry.waiting.add(1, {"mode": mode})
            try:
                with telemetry.tracer.start_as_current_span("wait_slot"):
                    wait_started = time.monotonic()
                    await _slots.acquire()
            finally:
                telemetry.waiting.add(-1, {"mode": mode})
            telemetry.slot_wait.record(time.monotonic() - wait_started, {"mode": mode})

            started = time.monotonic()
            telemetry.in_flight.add(1, {"mode": mode})
            try:
                outcome = await _download_and_send(
                    message, progress, url, fmt, mode, workdir, span, silent
                )
            finally:
                telemetry.in_flight.add(-1, {"mode": mode})
                _slots.release()
        except Exception as exc:
            error_type = classify_error(exc)
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            LOG.exception("failed to handle %s (%s)", url, error_type)
            with contextlib.suppress(TelegramError):
                await progress.edit_text("🚫 Sorry, mate, seems I shat my pants on that one")
        finally:
            span.set_attribute("download.outcome", outcome)
            result = {"outcome": outcome}
            if error_type:
                span.set_attribute("error.type", error_type)
                result["error.type"] = error_type
            telemetry.downloads.add(1, {**attrs, **result})
            if started is not None:
                telemetry.download_duration.record(
                    time.monotonic() - started, {"mode": mode, **result}
                )
            shutil.rmtree(workdir, ignore_errors=True)


# Ordered: the first match wins. The texts come from yt-dlp DownloadError
# messages. The result is a metric attribute, so keep the set small.
_ERROR_PATTERNS = (
    ("format", ("requested format is not available", "no video formats found")),
    # A post with a link but no video. Normal in silent mode.
    ("no_media", ("no video could be found", "no media found", "there is no video", "no video in")),
    ("rate_limited", ("http error 429", "too many requests", "rate-limit", "rate limit")),
    ("login_required", (
        "sign in", "log in", "login", "cookies", "private", "age-restricted",
        # Instagram: "This content isn't available to everyone: It can't be
        # seen by certain audiences."
        "available to everyone", "certain audiences",
    )),
    ("unavailable", ("unavailable", "removed", "deleted", "does not exist", "not found", "404")),
    ("unsupported", ("unsupported url",)),
)


def classify_error(exc: BaseException) -> str:
    """A low-cardinality label for an exception, for metrics and spans."""
    if isinstance(exc, TelegramError):
        return "telegram"
    if isinstance(exc, yt_dlp.utils.DownloadError):
        text = str(exc).lower()
        for label, needles in _ERROR_PATTERNS:
            if any(needle in text for needle in needles):
                return label
        return "download_other"
    return "other"


async def _download_and_send(
    message, progress, url, fmt, mode, workdir, span, silent=False
) -> str:
    """Returns the outcome label. Raises on unexpected failure."""
    loop = asyncio.get_running_loop()

    url_key = (mode, url)
    if await _send_cached(message, progress, url_key, mode):
        return "cached"

    # Spans go around the awaits, not into the pool threads, so the context
    # does not need to go to the threads.
    with telemetry.tracer.start_as_current_span("probe"):
        info = await loop.run_in_executor(_pool, _probe, url, fmt) or {}
    # The same video can have many URLs (youtu.be, m.youtube.com, &t=...).
    id_key = (mode, f"{info.get('extractor_key')}:{info.get('id')}") if info.get("id") else None
    if id_key and await _send_cached(message, progress, id_key, mode):
        _file_ids.put(url_key, _file_ids.get(id_key))
        return "cached"

    estimated = info.get("filesize") or info.get("filesize_approx") or 0
    span.set_attribute("download.estimated_bytes", estimated)

    # Only trust the estimate to reject early -- never to skip the real check.
    if estimated > MAX_TG_FILE_SIZE:
        await _reject_too_large(progress, estimated)
        return "too_large"

    hook = _progress_hook(progress, loop)
    with telemetry.tracer.start_as_current_span("download"):
        await loop.run_in_executor(_pool, _download, info, fmt, mode, workdir, hook)

    path = _downloaded_file(workdir)
    if path is None:
        raise RuntimeError(f"yt-dlp produced no file in {workdir}")

    size = os.path.getsize(path)
    span.set_attribute("download.actual_bytes", size)
    telemetry.download_size.record(size, {"mode": mode})
    if size > MAX_TG_FILE_SIZE:
        await _reject_too_large(progress, size)
        return "too_large"

    with telemetry.tracer.start_as_current_span("media_meta"):
        meta = await loop.run_in_executor(_pool, _media_meta, path, mode, info, workdir)

    if silent:
        # The only message in silent mode, and only after a successful download.
        # It is deleted after the upload, also when the upload fails.
        progress = await message.reply_text(
            "⬆️ Uploading...", reply_to_message_id=message.message_id
        )
    else:
        await progress.edit_text("⬆️ Uploading...")
    if TELEGRAM_LOCAL_MODE:
        # The local server reads the file itself; it runs as a different user.
        os.chmod(workdir, 0o755)
    try:
        with telemetry.tracer.start_as_current_span("upload") as upload:
            upload.set_attribute("download.actual_bytes", size)
            sent = await _send(message, mode, Path(path), **meta)
    except Exception:
        if silent:
            with contextlib.suppress(TelegramError):
                await progress.delete()
        raise
    file_id = _sent_file_id(sent)
    if file_id:
        for key in (url_key, id_key):
            if key:
                _file_ids.put(key, file_id)
    with contextlib.suppress(TelegramError):
        await progress.delete()
    return "ok"


async def _send_cached(message, progress, key, mode) -> bool:
    file_id = _file_ids.get(key)
    if file_id is None:
        return False
    try:
        with telemetry.tracer.start_as_current_span("send_cached"):
            await _send(message, mode, file_id)
    except BadRequest:
        # Telegram can forget a file_id. Then download the file again.
        LOG.warning("cached file_id for %s was refused, downloading again", key)
        _file_ids.drop(key)
        return False
    with contextlib.suppress(TelegramError):
        await progress.delete()
    return True


async def _send(message, mode, media, **meta):
    # A local Bot API server answers only after it has uploaded the file to
    # Telegram, so the read timeout must be as long as the upload.
    timeouts = {"read_timeout": UPLOAD_TIMEOUT, "write_timeout": UPLOAD_TIMEOUT}
    if mode == "audio":
        return await message.reply_audio(
            audio=media, reply_to_message_id=message.message_id, **timeouts, **meta
        )
    return await message.reply_video(
        video=media,
        supports_streaming=True,
        reply_to_message_id=message.message_id,
        **timeouts,
        **meta,
    )


def _sent_file_id(sent) -> str | None:
    # Telegram can store a short silent video as an animation or a document.
    media = sent.video or sent.audio or sent.animation or sent.document
    return media.file_id if media else None


async def _reject_too_large(progress, size: int) -> None:
    await progress.edit_text(
        f"🚫 File size is {size // 1024 // 1024}MB, which is over "
        f"Telegram's limit for bots ({MAX_TG_FILE_SIZE // 1024 // 1024}MB)"
    )


def _progress_hook(progress, loop):
    """Throttled by both percentage and wall time, to stay under Telegram's edit limits."""
    state = {"pct": -100, "at": 0.0}

    def hook(d):
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        if not total:
            return
        pct = int(d.get("downloaded_bytes", 0) / total * 100)
        now = time.monotonic()
        if pct - state["pct"] < 10 or now - state["at"] < 3:
            return
        state["pct"], state["at"] = pct, now
        asyncio.run_coroutine_threadsafe(_edit(progress, f"⬇️ Downloading... ({pct}%)"), loop)

    return hook


async def _edit(progress, text: str) -> None:
    # Edits race with the upload finishing and with Telegram's flood limits;
    # neither is worth failing a download over.
    with contextlib.suppress(TelegramError):
        await progress.edit_text(text)


def _probe(url: str, fmt: str) -> dict:
    with yt_dlp.YoutubeDL({"quiet": True, "format": fmt, "noprogress": True}) as ydl:
        return ydl.sanitize_info(ydl.extract_info(url, download=False))


def _download(info: dict, fmt: str, mode: str, workdir: str, hook) -> None:
    """Downloads from the info of _probe, so the site is not extracted a second time."""
    options = {
        # Each download gets its own directory, so the produced file can just be
        # discovered afterwards instead of tracked through postprocessor hooks.
        "outtmpl": os.path.join(workdir, "%(title).80B.%(ext)s"),
        "format": fmt,
        "progress_hooks": [hook],
        "quiet": True,
        "noprogress": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
        if mode == "audio"
        else [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.process_ie_result(info, download=True)


def _media_meta(path: str, mode: str, info: dict, workdir: str) -> dict:
    """Keyword arguments for reply_video or reply_audio. A failure gives fewer of them."""
    meta = {}
    try:
        probe = _ffprobe(path)
    except (OSError, subprocess.SubprocessError, ValueError):
        LOG.warning("ffprobe failed for %s", path, exc_info=True)
        return meta

    duration = float(probe.get("format", {}).get("duration") or 0)
    if duration:
        meta["duration"] = round(duration)

    if mode == "audio":
        title = info.get("track") or info.get("title")
        performer = info.get("artist") or info.get("uploader")
        if title:
            meta["title"] = title
        if performer:
            meta["performer"] = performer
        return meta

    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video" and stream.get("width"):
            meta["width"], meta["height"] = stream["width"], stream["height"]
            break
    thumbnail = _make_thumbnail(path, os.path.join(workdir, "thumbnail.jpg"), duration)
    if thumbnail:
        meta["thumbnail"] = Path(thumbnail)
    return meta


def _ffprobe(path: str) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def _make_thumbnail(path: str, thumbnail: str, duration: float) -> str | None:
    # Skip the first frames, which are frequently black.
    at = min(1.0, duration / 2)
    scale = (
        f"scale={THUMBNAIL_MAX_PX}:{THUMBNAIL_MAX_PX}:force_original_aspect_ratio=decrease"
    )
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", str(at), "-i", path,
             "-frames:v", "1", "-vf", scale, "-q:v", "5", thumbnail],
            capture_output=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        LOG.warning("thumbnail failed for %s", path, exc_info=True)
        return None
    if not os.path.isfile(thumbnail) or os.path.getsize(thumbnail) > THUMBNAIL_MAX_BYTES:
        return None
    return thumbnail


def _downloaded_file(workdir: str) -> str | None:
    candidates = [
        os.path.join(workdir, name)
        for name in os.listdir(workdir)
        if not name.endswith(".part")
    ]
    candidates = [p for p in candidates if os.path.isfile(p)]
    if not candidates:
        return None
    # Postprocessing can leave the pre-conversion input behind; the payload is
    # always the largest file.
    return max(candidates, key=os.path.getsize)


async def clean_orphans() -> None:
    while True:
        try:
            cutoff = time.time() - ORPHAN_TTL.total_seconds()
            for name in os.listdir(TMP_DIR):
                if name == ".gitkeep":
                    continue
                path = os.path.join(TMP_DIR, name)
                try:
                    if os.stat(path).st_mtime > cutoff:
                        continue
                except FileNotFoundError:
                    continue
                LOG.info("removing orphaned %s", path)
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    with contextlib.suppress(OSError):
                        os.unlink(path)
        except Exception:
            # A janitor crash used to kill the task silently and leak disk forever.
            LOG.exception("janitor pass failed")
        await asyncio.sleep(JANITOR_INTERVAL.total_seconds())


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOG.error("unhandled error", exc_info=context.error)


if __name__ == "__main__":
    main()
