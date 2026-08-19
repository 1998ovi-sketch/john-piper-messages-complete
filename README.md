# John Piper Messages — Complete Archive

This project builds a public RSS feed of audio-bearing John Piper resources from Desiring God's yearly **Messages** archive. It preserves the resource's original message date in RSS `pubDate`; it does not copy or re-host audio.

## Run locally

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python build_feed.py
```

On Windows, activate the virtual environment with `.venv\\Scripts\\Activate.ps1`, then run `python build_feed.py`.

The full crawl starts at the current year and probes back to 1900 (a safety floor, not an assumed start date). It handles a final archive-page 404, repeated pages, HTTP failures, and a per-year pagination safety cap. The persistent `catalog.json` retains already verified entries if the source changes. `unresolved_messages.json` records pages or audio URLs that could not be resolved; those items do not stop the build.

The generated feed is `public/john-piper-messages-complete.rss`; `public/index.html` links to it. A build refuses to publish an obviously tiny (fewer than ten verified items) RSS file and validates XML before reporting success.

## GitHub Pages

The included workflow runs daily and manually, uses Python 3.12, commits refreshed catalog/feed data, and deploys `public/` using GitHub Pages. After the repository is pushed, the feed URL will be:

`https://<OWNER>.github.io/<REPOSITORY>/john-piper-messages-complete.rss`

Desiring God remains the source of truth for page content and audio files. Audio enclosures point directly to their original Desiring God URLs.
