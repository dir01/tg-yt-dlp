import asyncio
import contextlib
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import yt_dlp
from aiohttp import web
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import telemetry

LOG = logging.getLogger("tg-yt-dlp")

URL_RE = re.compile(r"https?://[^\s<>\"']+")

MAX_TG_FILE_SIZE = int(os.environ.get("MAX_TG_FILE_SIZE", 50 * 1024 * 1024))
TMP_DIR = os.environ.get("TMP_DIR") or os.path.join(os.path.dirname(__file__), "tmp")
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", 9100))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 2))

# Orphans only: the happy path deletes its own workdir. This is the safety net
# for crashes, and the TTL is generous enough never to race a live download.
ORPHAN_TTL = timedelta(minutes=int(os.environ.get("ORPHAN_TTL_MINUTES", 60)))
JANITOR_INTERVAL = timedelta(minutes=10)

VIDEO_FORMAT = "bestvideo[ext=mp4][vcodec=avc1.4D401F]+bestaudio[ext=m4a]/best"
AUDIO_FORMAT = "bestaudio/best"

HELP_TEXT = (
    "YOU SEND LINK, ME SENDS VIDEO. YOU SEND /audio LINK, ME SENDS AUDIO. "
    "TELEGRAM LIMIT 50MB, C'EST LA VIE"
)

# yt-dlp is blocking and CPU/IO heavy; bound it so one busy chat can't spawn an
# unbounded number of ffmpeg processes. The semaphore bounds queueing, the pool
# bounds the threads themselves.
_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS, thread_name_prefix="ytdlp")
_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    telemetry.setup()
    os.makedirs(TMP_DIR, exist_ok=True)

    app = (
        ApplicationBuilder()
        .token(os.environ["TELEGRAM_TOKEN"])
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("start", handle_help))
    app.add_handler(CommandHandler("audio", handle_url))
    app.add_handler(MessageHandler(filters=filters.TEXT & ~filters.COMMAND, callback=handle_url))
    app.add_error_handler(handle_error)

    # run_polling installs its own SIGINT/SIGTERM handlers and drives shutdown
    # through post_shutdown, so there is nothing to unwind here.
    app.run_polling()


async def _post_init(app) -> None:
    app.bot_data["janitor"] = asyncio.create_task(clean_orphans())
    runner = web.AppRunner(_health_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HEALTH_PORT).start()
    app.bot_data["health_runner"] = runner
    LOG.info("health server on :%s, %s download slots", HEALTH_PORT, MAX_CONCURRENT_DOWNLOADS)


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


def _health_app() -> web.Application:
    async def ok(_request):
        return web.json_response({"status": "ok", "yt_dlp": yt_dlp.version.__version__})

    app = web.Application()
    app.router.add_get("/health", ok)
    # Kept so the existing k8s probes (which point at /metrics from the
    # aioprometheus days) keep passing through the rollout. Remove once the
    # deployment manifest has moved to /health.
    app.router.add_get("/metrics", ok)
    return app


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telemetry.messages.add(1, {"handler": "help"})
    await update.message.reply_text(HELP_TEXT)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    text = message.text or ""

    if text.startswith("/audio"):
        mode, fmt = "audio", AUDIO_FORMAT
        text = text[len("/audio"):]
    else:
        mode, fmt = "video", VIDEO_FORMAT
    telemetry.messages.add(1, {"handler": mode})

    # Match the URL out of the message rather than assuming the whole message is
    # one, so "look at this <url>" works.
    match = URL_RE.search(text)
    if not match:
        telemetry.downloads.add(1, {"mode": mode, "outcome": "invalid_url"})
        await message.reply_text(
            "Invalid URL. Please send a valid video URL",
            reply_to_message_id=message.message_id,
        )
        return

    url = match.group(0)
    domain = telemetry.bucket_domain(urlparse(url).netloc)
    attrs = {"mode": mode, "domain": domain}

    progress = await message.reply_text(
        "⬇️ Downloading...", reply_to_message_id=message.message_id
    )
    workdir = tempfile.mkdtemp(dir=TMP_DIR)
    started = time.monotonic()
    telemetry.in_flight.add(1, {"mode": mode})

    try:
        with telemetry.tracer.start_as_current_span("handle_url") as span:
            span.set_attribute("download.mode", mode)
            span.set_attribute("url.domain", domain)

            async with _slots:
                outcome = await _download_and_send(
                    message, progress, url, fmt, mode, workdir, span
                )

            span.set_attribute("download.outcome", outcome)
        telemetry.downloads.add(1, {**attrs, "outcome": outcome})
    except Exception:
        telemetry.downloads.add(1, {**attrs, "outcome": "error"})
        LOG.exception("failed to handle %s", url)
        with contextlib.suppress(TelegramError):
            await progress.edit_text("🚫 Sorry, mate, seems I shat my pants on that one")
    finally:
        telemetry.in_flight.add(-1, {"mode": mode})
        telemetry.download_duration.record(time.monotonic() - started, {"mode": mode})
        shutil.rmtree(workdir, ignore_errors=True)


async def _download_and_send(message, progress, url, fmt, mode, workdir, span) -> str:
    """Returns the outcome label. Raises on unexpected failure."""
    loop = asyncio.get_running_loop()

    info = await loop.run_in_executor(_pool, _probe, url, fmt) or {}
    estimated = info.get("filesize") or info.get("filesize_approx") or 0
    span.set_attribute("download.estimated_bytes", estimated)

    # Only trust the estimate to reject early -- never to skip the real check.
    if estimated > MAX_TG_FILE_SIZE:
        await _reject_too_large(progress, estimated)
        return "too_large"

    hook = _progress_hook(progress, loop)
    await loop.run_in_executor(_pool, _download, url, fmt, mode, workdir, hook)

    path = _downloaded_file(workdir)
    if path is None:
        raise RuntimeError(f"yt-dlp produced no file in {workdir}")

    size = os.path.getsize(path)
    span.set_attribute("download.actual_bytes", size)
    if size > MAX_TG_FILE_SIZE:
        await _reject_too_large(progress, size)
        return "too_large"

    await progress.edit_text("⬆️ Uploading...")
    with open(path, "rb") as f:
        if mode == "audio":
            await message.reply_audio(audio=f, reply_to_message_id=message.message_id)
        else:
            await message.reply_video(video=f, reply_to_message_id=message.message_id)
    with contextlib.suppress(TelegramError):
        await progress.delete()
    return "ok"


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
        return ydl.extract_info(url, download=False)


def _download(url: str, fmt: str, mode: str, workdir: str, hook) -> None:
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
        ydl.download([url])


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
