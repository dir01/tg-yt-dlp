# tg-yt-dlp
Telegram bot that wraps [yt-dlp](https://github.com/yt-dlp/yt-dlp), which is a fork of [youtube-dl](https://github.com/ytdl-org/youtube-dl), which is a program that downloads videos from YouTube and a few hundred other sites.

## What's it for?
Ever wanted to forward your friends that hilarious TikTok or YouTube short or a video on Twitter?
This is the use case I had in mind when I wrote it

## What's the selling point?
Thanks to Dependabot and GitHub Actions, this bot will always be up-to-date with the latest version of [yt-dlp](https://github.com/yt-dlp/yt-dlp). 
Whenever new version of [yt-dlp](https://github.com/yt-dlp/yt-dlp), the following will happen:
1. Dependabot will open a pull request to update the version of [yt-dlp](https://github.com/yt-dlp/yt-dlp) in the `pyproject.toml` file
2. GitHub action will automatically merge this pull request
3. GitHub action will automatically build a new docker image
4. (Irrelevant to anyone but me) [argocd-image-updater](https://argocd-image-updater.readthedocs.io/en/stable/) in my cluster will detect this and roll out new version

It's also a very small project, just around 100 lines of code

## Limitations and Downsides
1. Telegram has a 50MB file size limit, so this bot will not download videos larger than that
2. I did not write any test, so the whole automatic update is YOLO type of thing

## License
MIT License
