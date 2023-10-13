import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor

import yt_dlp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackContext, MessageHandler, filters

URL_RE = re.compile(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+')


def main():
    token = os.environ['TELEGRAM_TOKEN']
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("start", handle_help))
    app.add_handler(MessageHandler(filters=filters.Regex('.*'), callback=handle_url))
    app.run_polling()


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f'''
/video <URL> - download and send video
''')


async def handle_url(update: Update, context: CallbackContext):
    url = update.message.text
    if not is_url(url):
        await update.message.reply_text("Invalid URL. Please send a valid video URL.")
        return

    def _download_video(url_: str) -> str:
        with yt_dlp.YoutubeDL({
            'outtmpl': '%(title)s.%(ext)s',
            'format': 'bestvideo[ext=mp4][vcodec=avc1.4D401F]+bestaudio[ext=m4a]/best',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'quiet': True
        }) as ydl:
            info_dict = ydl.extract_info(url_, download=True)
            filename_ = ydl.prepare_filename(info_dict)
        return filename_

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        filename = await loop.run_in_executor(pool, _download_video, url)

    with open(filename, 'rb') as f:
        await update.message.reply_video(video=f, reply_to_message_id=update.message.message_id)

    os.remove(filename)


def is_url(url: str) -> bool:
    return bool(re.search(URL_RE, url))


if __name__ == '__main__':
    main()
