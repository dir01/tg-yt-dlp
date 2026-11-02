import functools
import http.server
import shutil
import subprocess
import threading

import pytest

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are necessary",
)


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory):
    """A 3 second 320x240 H.264 clip with AAC audio."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is necessary")
    path = tmp_path_factory.mktemp("sample") / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
    )
    return path


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def sample_url(sample_video):
    """The sample clip on a loopback HTTP server."""
    handler = functools.partial(_QuietHandler, directory=str(sample_video.parent))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/{sample_video.name}"
    server.shutdown()
