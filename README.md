# tg-yt-dlp
Telegram bot that wraps [yt-dlp](https://github.com/yt-dlp/yt-dlp), which is a fork of [youtube-dl](https://github.com/ytdl-org/youtube-dl), which is a program that downloads videos from YouTube and a few hundred other sites.

## What's it for?
Ever wanted to forward your friends that hilarious TikTok or YouTube short or a video on Twitter?
This is the use case I had in mind when I wrote it

## What's the selling point?
This bot stays up-to-date with the latest version of [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Every day, [`release.yml`](.github/workflows/release.yml):
1. Bumps `yt-dlp` to the newest release in `uv.lock`
2. Runs a smoke test against the new version — if it can't download and post-process, nothing ships
3. Merges the lockfile change through a pull request and builds a multi-arch image to `ghcr.io`
4. (Irrelevant to anyone but me) [argocd-image-updater](https://argocd-image-updater.readthedocs.io/en/stable/) in my cluster detects the new `sha-*` tag and rolls it out


## Observability
Metrics and traces are exported over OTLP, configured through the standard `OTEL_*` environment
variables (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`). With no endpoint configured the
instrumentation is a no-op, so local runs need no collector.

`tg_yt_dlp.build.timestamp` reports when the running image was built — alert on it aging past a few
days to catch a stalled release pipeline.

`GET /health` on port 9100 backs the k8s probes. It returns 503 when Telegram did not answer a
`getUpdates` call for `MAX_POLL_AGE_SECONDS`, so a liveness probe restarts a bot that cannot poll.

## Runtime
Python 3.15 on Alpine, built in two stages: the compiler toolchain lives in the builder and only the
resulting virtualenv, ffmpeg, and the two source files are copied into the image, which runs as an
unprivileged user. Dependencies are locked in `uv.lock`; `.python-version` pins the interpreter for
local `uv run` to the same version the image ships.

## Configuration
| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | *required* | Bot token |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Simultaneous yt-dlp jobs |
| `MAX_TG_FILE_SIZE` | 50 MB, or 2000 MB with `TELEGRAM_API_URL` | Upload size ceiling, in bytes |
| `TELEGRAM_API_URL` | *unset* | URL of a local Bot API server, for example `http://telegram-bot-api:8081` |
| `TELEGRAM_LOCAL_MODE` | `false` | Send file paths to the local server, not the file data. See below |
| `UPLOAD_TIMEOUT_SECONDS` | `600` | Write timeout for uploads |
| `FILE_ID_CACHE_SIZE` | `1000` | Number of sent files that the bot can send again without a download |
| `MAX_POLL_AGE_SECONDS` | `120` | `/health` returns 503 after this time without a `getUpdates` answer |
| `TMP_DIR` | `./tmp` | Scratch space for downloads |
| `HEALTH_PORT` | `9100` | Health endpoint port |
| `LOG_LEVEL` | `INFO` | Python log level |

## Files larger than 50 MB
The cloud Bot API accepts uploads up to 50 MB. A local
[Bot API server](https://github.com/tdlib/telegram-bot-api) that runs with `--local` accepts
uploads up to 2000 MB. To use it:

1. Get `api_id` and `api_hash` from https://my.telegram.org.
2. Log the bot out of the cloud API one time:
   `curl https://api.telegram.org/bot$TELEGRAM_TOKEN/logOut`.
   If you do not do this step, the local server cannot receive updates for the bot.
3. Run the server next to the bot, and set `TELEGRAM_API_URL`. Example with Docker Compose:

```yaml
services:
  telegram-bot-api:
    image: aiogram/telegram-bot-api:latest
    environment:
      TELEGRAM_API_ID: "..."
      TELEGRAM_API_HASH: "..."
      TELEGRAM_LOCAL: "1"
    volumes:
      - downloads:/app/tmp
  bot:
    image: ghcr.io/dir01/tg-yt-dlp:latest
    environment:
      TELEGRAM_TOKEN: "..."
      TELEGRAM_API_URL: http://telegram-bot-api:8081
      TELEGRAM_LOCAL_MODE: "true"
    volumes:
      - downloads:/app/tmp
volumes:
  downloads:
```

With `TELEGRAM_LOCAL_MODE`, the bot sends only the file path, and the server reads the file from
the shared volume. The volume must have the same path in the two containers. Without
`TELEGRAM_LOCAL_MODE`, the bot uploads the file over HTTP, and python-telegram-bot keeps the full
file in memory during the upload.

Large files also need disk space in `TMP_DIR`: about two times the file size, because the
postprocessors write a new file.

## Repeated links
The bot keeps the Telegram `file_id` of each file that it sent, for the URL and for the video ID.
When a user sends the same video again, the bot sends the stored `file_id`, without a download.
This store is in memory, so a restart clears it.

## Tests
`uv run pytest` runs the unit tests. Some tests need `ffmpeg` and `ffprobe`; without them, these
tests are skipped. CI runs the unit tests and the smoke test on each pull request.

## Limitations and Downsides
1. Without a local Bot API server, Telegram limits bot uploads to 50 MB, so the bot does not send
   larger files
2. The smoke test is hermetic (ffmpeg generates a clip, yt-dlp fetches it over loopback), so it
   catches broken releases but not breakage in any individual site's extractor

## License
MIT License
