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
3. Commits the lockfile and builds a multi-arch image to `ghcr.io`
4. (Irrelevant to anyone but me) [argocd-image-updater](https://argocd-image-updater.readthedocs.io/en/stable/) in my cluster detects the new `sha-*` tag and rolls it out

This path runs entirely on `GITHUB_TOKEN`. An earlier version relied on Dependabot plus an
auto-merge personal access token; the token expired, the chain broke silently, and the bot ran a
14-month-old yt-dlp before anyone noticed. Hence also the staleness metric below.

Dependabot still manages everything *except* yt-dlp, and those PRs auto-merge and get picked up by
the next daily build.

## Observability
Metrics and traces are exported over OTLP, configured through the standard `OTEL_*` environment
variables (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`). With no endpoint configured the
instrumentation is a no-op, so local runs need no collector.

`tg_yt_dlp.build.timestamp` reports when the running image was built — alert on it aging past a few
days to catch a stalled release pipeline.

`GET /health` on port 9100 backs the k8s probes.

## Configuration
| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | *required* | Bot token |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Simultaneous yt-dlp jobs |
| `MAX_TG_FILE_SIZE` | `52428800` | Upload size ceiling |
| `TMP_DIR` | `./tmp` | Scratch space for downloads |
| `HEALTH_PORT` | `9100` | Health endpoint port |
| `LOG_LEVEL` | `INFO` | Python log level |

## Limitations and Downsides
1. Telegram has a 50MB file size limit for bots, so this bot will not send videos larger than that
2. The smoke test is hermetic (ffmpeg generates a clip, yt-dlp fetches it over loopback), so it
   catches broken releases but not breakage in any individual site's extractor

## License
MIT License
