import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor

import yt_dlp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackContext, MessageHandler, filters

URL_RE = re.compile(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+')
MAX_TG_FILE_SIZE = 50 * 1024 * 1024  # 50MB


def main():
    token = os.environ['TELEGRAM_TOKEN']
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("start", handle_help))
    app.add_handler(MessageHandler(filters=filters.Regex('.*'), callback=handle_url))
    app.run_polling()


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('YOU SEND LINK, ME SENDS VIDEO. TELEGRAM LIMIT VIDEO 50MB, SE LA VIE')


async def handle_url(update: Update, context: CallbackContext):
    url = update.message.text
    if not is_url(url):
        await update.message.reply_text("Invalid URL. Please send a valid video URL",
                                        reply_to_message_id=update.message.message_id)
        return

    progress_msg = await update.message.reply_text("⬇️ Downloading...", reply_to_message_id=update.message.message_id)

    loop = asyncio.get_event_loop()

    prev_percent = 0
    def _update_progress_msg(d):
        nonlocal prev_percent
        if d['status'] == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            percent = int(downloaded / total * 100 if total else 0)
            if percent - prev_percent < 5:
                return
            prev_percent = percent
            new_text = f"⬇️ Downloading... ({percent}%)"
            asyncio.run_coroutine_threadsafe(progress_msg.edit_text(new_text), loop)

    def _get_info(url_: str) -> dict:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            return ydl.extract_info(url_, download=False)

    def _download(url_: str) -> str:
        with yt_dlp.YoutubeDL({
            'outtmpl': '%(title)s.%(ext)s',
            'format': 'bestvideo[ext=mp4][vcodec=avc1.4D401F]+bestaudio[ext=m4a]/best',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'progress_hooks': [_update_progress_msg],
            'quiet': True
        }) as ydl:
            info_dict = ydl.extract_info(url_, download=True)
            filename_ = ydl.prepare_filename(info_dict)
        return filename_

    with ThreadPoolExecutor() as pool:
        info = await loop.run_in_executor(pool, _get_info, url) or {}

        file_size = info.get('filesize', 0) or info.get('filesize_approx', 0) or 0
        filename = None

        if not file_size:
            filename = await loop.run_in_executor(pool, _download, url)
            file_size = os.path.getsize(filename)

        if file_size > MAX_TG_FILE_SIZE:
            file_size_mb = int(file_size / 1024 / 1024)
            await update.message.reply_text(
                f"🚫 File size is {file_size_mb}MB, which is over Telegram's limit for bots (50MB)"
            )
            return

        if not filename:
            filename = await loop.run_in_executor(pool, _download, url)

    await progress_msg.edit_text("⬆️ Uploading...")

    with open(filename, 'rb') as f:
        await update.message.reply_video(video=f, reply_to_message_id=update.message.message_id)

    await progress_msg.delete()
    os.remove(filename)


def is_url(url: str) -> bool:
    return bool(re.search(URL_RE, url))


if __name__ == '__main__':
    main()
