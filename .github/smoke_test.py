"""Gate a yt-dlp bump on it still being able to do what this bot needs.

Hermetic by design: ffmpeg synthesises a short clip, a loopback HTTP server
serves it, and yt-dlp fetches it from there. Nothing outside the runner is
touched, so this test can only fail for reasons that are actually yt-dlp's
fault -- no third-party host going 403, no CI IP getting bot-blocked.

What it covers: the package imports, the generic extractor works, both of the
bot's format selectors resolve, the downloader writes a file, and the ffmpeg
postprocessor chain (mp4 remux, mp3 extraction) still runs.

What it does not cover: site-specific extractors. Testing those from CI means
depending on hosts that block datacenter IPs, which produces false failures
that block legitimate updates -- worse than no gate.
"""

import functools
import http.server
import os
import subprocess
import sys
import tempfile
import threading

import yt_dlp

VIDEO_FORMAT = "bestvideo[ext=mp4][vcodec=avc1.4D401F]+bestaudio[ext=m4a]/best"
AUDIO_FORMAT = "bestaudio/best"

CASES = [
    ("video", VIDEO_FORMAT, [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}]),
    (
        "audio",
        AUDIO_FORMAT,
        [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    ),
]


def make_sample(path: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            path,
        ],
        check=True,
    )


def serve(directory: str) -> tuple[str, http.server.ThreadingHTTPServer]:
    handler = functools.partial(QuietHandler, directory=directory)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/sample.mp4", server


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def run_case(url: str, name: str, fmt: str, postprocessors: list) -> None:
    with tempfile.TemporaryDirectory() as workdir:
        options = {
            "outtmpl": os.path.join(workdir, "%(title).80B.%(ext)s"),
            "format": fmt,
            "postprocessors": postprocessors,
            "quiet": True,
            "noprogress": True,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

        files = [f for f in os.listdir(workdir) if not f.endswith(".part")]
        if not files:
            sys.exit(f"FAIL [{name}]: yt-dlp produced no file")
        largest = max(files, key=lambda f: os.path.getsize(os.path.join(workdir, f)))
        size = os.path.getsize(os.path.join(workdir, largest))
        if size < 1024:
            sys.exit(f"FAIL [{name}]: {largest} is suspiciously small ({size} bytes)")
        print(f"ok [{name}]: {largest} ({size} bytes)")


def main() -> None:
    print(f"yt-dlp {yt_dlp.version.__version__}")
    with tempfile.TemporaryDirectory() as srcdir:
        make_sample(os.path.join(srcdir, "sample.mp4"))
        url, server = serve(srcdir)
        try:
            for name, fmt, postprocessors in CASES:
                run_case(url, name, fmt, postprocessors)
        finally:
            server.shutdown()
    print("smoke test passed")


if __name__ == "__main__":
    main()
