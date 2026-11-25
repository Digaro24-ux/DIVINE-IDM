# DIVINE IDM

A free, open-source download manager that runs as a web app. Inspired by
[Ghost Downloader 3](https://github.com/XiaoYouChR/Ghost-Downloader-3).

## Run it

    python3 server.py --open          # needs Python 3.8+, nothing else

Optional extras (video sites, torrents): `pip install -r requirements.txt`

Open http://localhost:6277. Windows: double-click `run.bat`. macOS/Linux: `./run.sh`.

## Feature status

| Feature | Status |
|---|---|
| Multi-connection downloads (up to 32 segments, retries) | Built in, tested |
| Pause / resume (survives restarts) | Built in, tested |
| HTTP / HTTPS | Built in, tested |
| FTP (resumable) | Built in, **not tested** against a real FTP server |
| Queue with max simultaneous downloads | Built in, tested |
| YouTube / Bilibili / 1000+ sites | Needs `pip install -U yt-dlp` (+ ffmpeg for merging / MP3). **Not tested here** |
| Torrents / magnet links | Needs `pip install libtorrent`. **Not tested here** |
| Browser integration | Extension in `extension/`, plus a bookmarklet. **Not tested in a real browser** |
| Windows / macOS / Linux | Python + any browser |
| Android | Run in Termux (`pkg install python; python server.py`) and open it in Chrome, then "Add to Home screen"; or run `--host 0.0.0.0` on a PC and open the printed URL on the phone |

The app shows which optional backends are missing (the yellow chips at the top), and
refuses those link types with an install hint instead of failing silently.

## Browser extension

1. Chrome/Edge/Brave: open `chrome://extensions`, enable Developer mode, **Load unpacked**, pick `extension/`.
2. Open the extension's options and paste the token printed by `server.py`.
3. Right-click any link/video, or just download something: large browser downloads are handed to DIVINE IDM.

## Security

* Binds to 127.0.0.1 by default and rejects other `Host` headers (anti DNS-rebinding).
* Every API call needs the secret token (stored in `~/.divine_idm/config.json`).
* `--host 0.0.0.0` exposes it to your network: anyone with the token controls downloads and the save folder. Use only on trusted networks.
* Only download content you have the right to download.

## Files

    server.py   HTTP API + queue scheduler
    engine.py   segmented HTTP/FTP downloader
    media.py    yt-dlp wrapper
    torrent.py  libtorrent wrapper
    static/     web UI (installable PWA)
    extension/  browser extension (Manifest V3)
