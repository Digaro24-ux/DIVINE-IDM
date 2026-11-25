"""YouTube / Bilibili / 1000+ other sites via yt-dlp (optional dependency)."""
import os
import re
import threading
import time

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None

AVAILABLE = yt_dlp is not None
INSTALL_HINT = "pip install -U yt-dlp   (and install ffmpeg to merge video+audio / extract mp3)"

MEDIA_HOSTS = re.compile(
    r"(^|\.)(youtube\.com|youtu\.be|bilibili\.com|b23\.tv|vimeo\.com|twitch\.tv|"
    r"dailymotion\.com|soundcloud\.com|tiktok\.com|twitter\.com|x\.com|instagram\.com|"
    r"facebook\.com|fb\.watch|reddit\.com|nicovideo\.jp)$", re.I)

FORMATS = {
    "best": "bv*+ba/b",
    "2160": "bv*[height<=2160]+ba/b[height<=2160]",
    "1440": "bv*[height<=1440]+ba/b[height<=1440]",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720": "bv*[height<=720]+ba/b[height<=720]",
    "480": "bv*[height<=480]+ba/b[height<=480]",
    "audio": "ba/b",
}


class _Stop(Exception):
    pass


def probe(url):
    if not AVAILABLE:
        raise RuntimeError("yt-dlp is not installed. " + INSTALL_HINT)
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True,
                           "skip_download": True, "socket_timeout": 20}) as ydl:
        info = ydl.extract_info(url, download=False)
    heights = sorted({f.get("height") for f in info.get("formats", []) if f.get("height")},
                     reverse=True)
    return {
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "heights": heights,
        "site": info.get("extractor_key"),
    }


class MediaDownload:
    def __init__(self, url, out_dir, quality="best"):
        if not AVAILABLE:
            raise RuntimeError("yt-dlp is not installed. " + INSTALL_HINT)
        self.url, self.out_dir, self.quality = url, out_dir, quality
        self.state = "queued"
        self.error = None
        self.path = None
        self.filename = None
        self.total = 0
        self.downloaded = 0
        self.speed = 0.0
        self._eta = None
        self._pause = threading.Event()
        self._thread = None

    @property
    def percent(self):
        return 100.0 * self.downloaded / self.total if self.total else 0.0

    @property
    def eta(self):
        return self._eta

    def start(self):
        if self.state == "running":
            return
        self._pause.clear()
        self.state, self.error = "running", None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self):
        if self.state == "running":
            self._pause.set()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def _hook(self, d):
        if self._pause.is_set():
            raise _Stop()
        if d["status"] == "downloading":
            self.downloaded = d.get("downloaded_bytes") or 0
            self.total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            self.speed = d.get("speed") or 0.0
            self._eta = d.get("eta")
            self.path = d.get("filename") or self.path
            self.filename = os.path.basename(self.path) if self.path else None

    def _run(self):
        os.makedirs(self.out_dir, exist_ok=True)
        opts = {
            "format": FORMATS.get(self.quality, FORMATS["best"]),
            "outtmpl": os.path.join(self.out_dir, "%(title).150B [%(id)s].%(ext)s"),
            "noplaylist": True, "continuedl": True, "retries": 10,
            "fragment_retries": 10, "socket_timeout": 20,
            "quiet": True, "no_warnings": True, "noprogress": True,
            "progress_hooks": [self._hook], "merge_output_format": "mp4",
        }
        if self.quality == "audio":
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                       "preferredcodec": "mp3", "preferredquality": "192"}]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
                rd = (info.get("requested_downloads") or [{}])[0]
                self.path = rd.get("filepath") or ydl.prepare_filename(info)
            self.filename = os.path.basename(self.path)
            if os.path.exists(self.path):
                self.total = self.downloaded = os.path.getsize(self.path)
            self.speed, self._eta, self.state = 0.0, None, "done"
        except _Stop:
            self.speed, self.state = 0.0, "paused"
        except Exception as e:  # noqa: BLE001
            if self._pause.is_set() or "_Stop" in repr(e):
                self.speed, self.state = 0.0, "paused"
            else:
                self.error, self.state = str(e), "error"
