import asyncio
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import urlparse

import yt_dlp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackContext,
    MessageHandler,
    filters,
)
from aioprometheus import Counter
from aioprometheus.service import Service

URL_RE = re.compile(
    r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
)
MAX_TG_FILE_SIZE = 50 * 1024 * 1024  # 50MB
TMP_DIR = os.path.join(os.path.dirname(__file__), "tmp")

messages_total = Counter(
    "tg_yt_dlp_messages_total",
    "Total number of messages",
)
filesize_limit_exceeded_total = Counter(
    "tg_yt_dlp_filesize_limit_exceeded_total",
    "Total number of URLs with file size over limit",
)
wrong_url_total = Counter(
    "tg_yt_dlp_wrong_url_total",
    "Total number of malformed/broken/unsupported URLs",
)
unknown_error_total = Counter(
    "tg_yt_dlp_unknown_error_total",
    "Total number of unknown errors",
)
link_domain_total = Counter(
    "tg_yt_dlp_link_domains_total",
    "Total number of links by domain",
)
metrics_service = Service()


def main():
    token = os.environ["TELEGRAM_TOKEN"]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    clean_task = loop.create_task(clean_old_files())
    loop.create_task(metrics_service.start(addr="0.0.0.0", port=9100))

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("start", handle_help))
    app.add_handler(CommandHandler("audio", handle_url))
    app.add_handler(MessageHandler(filters=filters.Regex(".*"), callback=handle_url))

    try:
        app.run_polling()
    except KeyboardInterrupt:
        loop.run_until_complete(app.stop())
        loop.run_until_complete(metrics_service.stop())
        clean_task.cancel()
        loop.run_until_complete(clean_task)
        raise


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    messages_total.inc({"handler": "help"})
    await update.message.reply_text(
        "YOU SEND LINK, ME SENDS VIDEO. YOU SEND /audio LINK, ME SENDS AUDIO. TELEGRAM LIMIT 50MB, SE LA VIE"
    )


async def handle_url(update: Update, context: CallbackContext):
    url = update.message.text
    download_mode = "video"
    file_extention = ".mp4"
    if url.startswith('/audio '):
        download_mode = "audio"
        file_extention = ".mp3"
        messages_total.inc({"handler": "audio"})
        url = url[7:]
    else:
        messages_total.inc({"handler": "url"})
    if not is_url(url):
        await update.message.reply_text(
            "Invalid URL. Please send a valid video URL",
            reply_to_message_id=update.message.message_id,
        )
        return

    link_domain_total.inc({"domain": urlparse(url).netloc})

    filepath = os.path.join(TMP_DIR, uuid.uuid4().hex + file_extention)
    progress_msg = await update.message.reply_text(
        "⬇️ Downloading...",
        reply_to_message_id=update.message.message_id,
    )

    loop = asyncio.get_event_loop()

    prev_percent = 0

    def _update_postprocessor(d):
        nonlocal filepath
        if d["info_dict"]["filepath"]:
            filepath = d["info_dict"]["filepath"]

    def _update_progress_msg(d):
        nonlocal prev_percent
        if d["status"] == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            percent = int(downloaded / total * 100 if total else 0)
            if percent - prev_percent < 5:
                return
            prev_percent = percent
            new_text = f"⬇️ Downloading... ({percent}%)"
            asyncio.run_coroutine_threadsafe(progress_msg.edit_text(new_text), loop)

    def _get_info(url_: str) -> dict:
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            return ydl.extract_info(url_, download=False)

    def _download(url_: str):
        yt_dlp_options = {
            "outtmpl": filepath,
            "format": "bestvideo[ext=mp4][vcodec=avc1.4D401F]+bestaudio[ext=m4a]/best",
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                }
            ],
            "progress_hooks": [_update_progress_msg],
            "postprocessor_hooks":[_update_postprocessor],
            "quiet": True,
        }
        if download_mode == "audio":
            yt_dlp_options["format"] = 'bestaudio/best'
            yt_dlp_options["postprocessors"] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        with yt_dlp.YoutubeDL(
            yt_dlp_options
        ) as ydl:
            ydl.download([url_])

    with ThreadPoolExecutor() as pool:
        try:
            info = await loop.run_in_executor(pool, _get_info, url) or {}

            file_size = info.get("filesize", 0) or info.get("filesize_approx", 0) or 0

            if not file_size:
                await loop.run_in_executor(pool, _download, url)
                file_size = os.path.getsize(filepath)

            if file_size > MAX_TG_FILE_SIZE:
                file_size_mb = int(file_size / 1024 / 1024)
                await update.message.reply_text(
                    f"🚫 File size is {file_size_mb}MB, which is over Telegram's limit for bots (50MB)"
                )
                filesize_limit_exceeded_total.inc({})
                return

            if not os.path.exists(filepath):
                await loop.run_in_executor(pool, _download, url)
        except Exception as e:
            unknown_error_total.inc({})
            await progress_msg.edit_text(
                f"🚫 Sorry, mate, seems I shat my pants on that one"
            )
            raise

    await progress_msg.edit_text("⬆️ Uploading...")

    with open(filepath, "rb") as f:
        if download_mode == 'video': 
            await update.message.reply_video(
                video=f,
                reply_to_message_id=update.message.message_id,
            )
        elif download_mode == 'audio':
            await update.message.reply_audio(
                audio=f,
                reply_to_message_id=update.message.message_id,
            )
    await progress_msg.delete()
    os.remove(filepath)


async def clean_old_files():
    while True:
        now = time.time()
        for filename in os.listdir(TMP_DIR):
            if filename == ".gitkeep":
                continue
            filepath = os.path.join(TMP_DIR, filename)
            stat = os.stat(filepath)
            if (now - stat.st_mtime) > timedelta(minutes=1).total_seconds():
                os.unlink(filepath)
        await asyncio.sleep(timedelta(minutes=2).total_seconds())


def is_url(url: str) -> bool:
    return bool(re.search(URL_RE, url))


if __name__ == "__main__":
    main()
