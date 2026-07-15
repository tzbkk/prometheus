# ⚠️ Legacy Scraper (inject.js)

This module (`src/prometheus/`) contains the **legacy** QQ scraper based on
AppImage injection (inject.js hooking JSON.parse inside QQ Electron).

## Status: Legacy

The **web scraper** (`src/web_scraper/`) is the new recommended approach.
It uses pure HTTP calls to pd.qq.com public APIs — no AppImage, no browser,
no QQ client required.

## When to use this legacy scraper

- The pd.qq.com web API stops working or changes
- You need features not yet available in the web scraper
- For backward compatibility with existing data collection

## How it works

1. Unpack QQ AppImage
2. Inject `inject.js` into QQ's renderer process
3. Hook `JSON.parse` to capture feed data as QQ scrolls
4. Data written to `feeds.jsonl`, `comments.jsonl`, `media/`

See `doc/ARCHITECTURE.md` for the full injection architecture.