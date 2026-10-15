# ⚡ DIVINE IDM

A multi-engine Internet Download Manager built with **Python + Flask** and a dark "download center" interface (IDM-style sidebar and table, card view, Parabolic-style Add Download dialog, VidMate-style quality picker).

One router sends every link to the right engine: direct files use a **multi-threaded Range engine**; video/music sites use **yt-dlp** with automatic fallback to **PyTube (pytubefix) → You-Get → Lux → gallery-dl**; image galleries use **gallery-dl**.

## Quick start

**Windows:** double-click `start.bat` (creates a virtual environment, installs everything, starts the app).
**macOS / Linux:** `./start.sh`

Or manually:

```bash
python -m venv venv
venv\Scripts\activate          # macOS/Linux: source venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

The terminal prints the exact address and your browser opens by itself, normally **http://127.0.0.1:5001**.
`python app.py --check` prints a diagnostic report. Options: `--port 8080`, `--no-browser`.

## Features

- **Interface:** sidebar categories (Musics, Compressed, Videos, Programs, Documents, APKs, Images) with counts, Unfinished/Finished, disk-space gauge; **List** and **Card** views; filter tabs (All / Downloading / Paused / Completed / Failed); search; checkboxes + toolbar (Resume, Pause, Stop All, Delete, Clear Done, To Phone, Options); right-click menu (Open, Open Folder, Preview, Rename, Send to phone, Pause/Resume, Redownload, Copy link, Remove, Delete file, Properties).
- **Add Download dialog:** thumbnail, title, duration, File Type (Video/Audio), quality grid (144P → 4K HD, 128K MP3, M4A...) with sizes, file name, save folder, free space, advanced options.
- **Multi-threaded engine:** pre-allocated file, 2–16 Range connections, pause/resume that survives restarts.
- **Exact file names** from `Content-Disposition` / URL; **rename** before or after downloading.
- **Everything saves to your real Downloads folder** (Windows folders moved to OneDrive/another drive are detected).
- **Phone-friendly videos:** VP9/AV1 + Opus (YouTube 1440p/4K) is converted to **H.264 + AAC MP4** only when needed, so Android Gallery and WhatsApp can play it.
- **Correct file date:** files get today's date (not the upload date), so they appear at the top of Gallery/Files.
- **Send to phone:** serves a finished file to your phone over Wi-Fi (QR code). The phone's browser downloads it, so Files, Gallery and WhatsApp see it.
- **Network doctor:** detects a dead system proxy (the usual cause of `WinError 10061`), switches to a direct connection automatically, and explains DNS/firewall problems. Manual proxy supported.
- **Settings:** simultaneous downloads (1–6), default connections, phone-friendly default, **"Only allow images, audio & video"** strict mode (PNG/JPG · MP3 · MP4), network mode.

## Troubleshooting

**The page loads but nothing works / "Backend offline".** Keep the terminal running `python app.py` open and use the address it prints. If port 5001 is busy or blocked (Windows reserves some ranges) the app uses 5002, 5003... automatically. The page shows a green/red *Backend connected* indicator. If the page is served from another origin, add `?api=http://127.0.0.1:5001` to its address (it is remembered). Windows: `netsh int ipv4 show excludedportrange protocol=tcp` lists blocked ports.

**`WinError 10061` / "actively refused it" for YouTube.** Open **Options → Network → Test connection**. Usually a leftover VPN/proxy setting: choose *Direct connection*. Otherwise allow `python.exe` in the firewall or pause antivirus web protection.

**YouTube gives few/no formats.** Update: `python -m pip install -U "yt-dlp[default]"`. Recent yt-dlp may need Deno or Node.js installed for YouTube.

**Downloaded files don't show in Gallery/WhatsApp.** Keep *Phone-friendly video* on, and use **Send to phone** (or copy into the phone's `Movies`/`Music`/`Pictures` folder, then reopen Gallery).

## Project structure

```
app.py            Flask server, REST API, launcher (port fallback, --check)
manager.py        DownloadManager: router, fallback chain, queue, bulk actions
router.py         URL classifier (direct / media / gallery)
direct_engine.py  Multi-threaded Range engine
media_engine.py   yt-dlp          pytube_engine.py  PyTube (pytubefix)
cli_engines.py    You-Get, Lux, gallery-dl
base.py           shared task logic (pause/resume/cancel/rename, phone-friendly step)
mediafix.py       H.264/AAC conversion      share.py     send-to-phone LAN server
network.py        proxy/DNS diagnostics     settings.py  persistent settings
utils.py, database.py
templates/index.html, static/css/style.css, static/js/app.js
start.bat, start.sh
```

## REST API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/health` | backend status |
| POST | `/api/inspect` `{url}` | classify link, list qualities (no download) |
| POST | `/api/download` `{url, option_id?, name?, threads?, compat?}` | start download |
| POST | `/api/pause·resume·cancel·redownload·rename·delete·open·share/<id>` | task actions |
| POST | `/api/pause-all` · `/api/resume-all` · `/api/clear-finished` | bulk |
| GET | `/api/downloads` · `/api/preview/<id>` · `/api/disk` · `/api/engines` | data |
| GET/POST | `/api/settings` · `/api/network` | settings |

## Notes

- Direct links accept any file type by default; turn on strict mode in Settings for images (PNG/JPG), audio (MP3) and video (MP4) only.
- Lux is a separate Go program; it is used only if `lux` is installed. DRM streams (Netflix etc.) are not supported.
- The app listens on this PC only (`127.0.0.1`). Only files you share are exposed (random link, 1 hour) while "Send to phone" is used.
- Only download content you have the right to download.

MIT License
