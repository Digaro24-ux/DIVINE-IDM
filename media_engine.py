"""Engine 1 - Streaming & media engine (yt-dlp).

inspect_media()  -> structured JSON (title, thumbnail, audio + video quality
                    options with sizes) WITHOUT downloading anything.
MediaTask        -> downloads the chosen option, with progress hooks,
                    pause/resume (yt-dlp continues .part files) and cancel.

yt-dlp itself handles DASH/HLS manifests, signature/JS decryption and
fragment downloading. Merging video+audio and MP3 conversion need `ffmpeg`.
"""
import os
import shutil
import time

import yt_dlp
from yt_dlp.utils import DownloadCancelled

from base import BaseTask
import network
from utils import ffmpeg_path, fmt_bytes, fmt_duration, fmt_eta

MP3_BITRATES = (320, 256, 192, 128, 64, 48)


class _Quiet:
    """Silent logger: keeps yt-dlp's colour codes out of the console and the UI."""
    def debug(self, msg): pass
    info = warning = error = debug


def base_opts():
    """Options shared by inspection and download."""
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "logger": _Quiet(),
         "color": {"stdout": "no_color", "stderr": "no_color"}, "socket_timeout": 20, "retries": 3,
         "updatetime": False}                      # keep file date = download date (Gallery sorts by it)
    proxy = network.ytdlp_proxy()
    if proxy is not None:
        o["proxy"] = proxy
    if ffmpeg_path():
        o["ffmpeg_location"] = ffmpeg_path()
    if shutil.which("node") and not shutil.which("deno"):
        o["js_runtimes"] = {"node": {}}           # YouTube needs a JS runtime (Deno is the default)
    return o


def _res_label(h):
    if h >= 4320: return "8K"
    if h >= 2160: return "4K Ultra HD"
    if h >= 1440: return "1440p 2K"
    if h >= 1080: return "1080p Full HD"
    if h >= 720:  return "720p HD"
    return f"{h}p"


def _has(codec):
    return codec not in (None, "", "none")


def _codec(c):
    return (c or "").split(".")[0]


def parse_info(info, ffmpeg):
    """Turn a yt-dlp info dict into the JSON structure the UI needs. (Pure function.)"""
    duration = info.get("duration") or 0
    formats = info.get("formats") or []

    def size_of(f):
        """-> (bytes|None, is_estimate)"""
        fs = f.get("filesize") or f.get("filesize_approx")
        if fs:
            return int(fs), False
        if f.get("tbr") and duration:
            return int(f["tbr"] * 1000 / 8 * duration), True
        return None, True

    # ----- audio-only streams -----
    a_only = [f for f in formats if _has(f.get("acodec")) and not _has(f.get("vcodec"))]
    best_a = max(a_only, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    best_a_size = size_of(best_a)[0] if best_a else 0

    audio, seen = [], set()
    if ffmpeg and duration:   # MP3 conversion: size = bitrate x duration
        for kbps in MP3_BITRATES:
            sz = int(kbps * 1000 / 8 * duration)
            audio.append({"id": f"a-mp3-{kbps}", "type": "audio", "label": f"MP3 {kbps} kbps",
                          "ext": "mp3", "abr": kbps, "codec": "mp3", "selector": "bestaudio/best",
                          "convert": {"codec": "mp3", "quality": str(kbps)},
                          "filesize": sz, "filesize_text": "~" + fmt_bytes(sz), "estimated": True})
    for f in sorted(a_only, key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True):
        abr = int(round(f.get("abr") or f.get("tbr") or 0))
        key = (f.get("ext"), abr)
        if key in seen or len(seen) >= 6:
            continue
        seen.add(key)
        sz, est = size_of(f)
        audio.append({"id": f"a-{f['format_id']}", "type": "audio",
                      "label": f"{(f.get('ext') or '').upper()} {abr} kbps" if abr else (f.get("ext") or "").upper(),
                      "ext": f.get("ext"), "abr": abr, "codec": _codec(f.get("acodec")),
                      "selector": f["format_id"], "convert": None, "filesize": sz,
                      "filesize_text": ("~" if est and sz else "") + fmt_bytes(sz), "estimated": est})

    # ----- video: best stream per resolution -----
    by_height = {}
    for f in formats:
        h = f.get("height")
        if not (_has(f.get("vcodec")) and h):
            continue
        progressive = _has(f.get("acodec"))
        if not progressive and not (ffmpeg and best_a):
            continue                        # can't merge without ffmpeg + an audio stream
        score = (str(f.get("vcodec", "")).startswith("avc1"), f.get("tbr") or 0)
        if h not in by_height or score > by_height[h][0]:
            by_height[h] = (score, f, progressive)

    video = []
    for h in sorted(by_height, reverse=True):
        _, f, progressive = by_height[h]
        sz, est = size_of(f)
        if sz and not progressive:
            sz += best_a_size or 0
        fps = f.get("fps")
        video.append({"id": f"v-{f['format_id']}", "type": "video",
                      "label": _res_label(h) + (f" {int(fps)}fps" if fps and fps > 30 else ""),
                      "height": h, "ext": "mp4" if not progressive else f.get("ext"),
                      "codec": _codec(f.get("vcodec")), "fps": fps,
                      "selector": f["format_id"] if progressive
                      else f"{f['format_id']}+bestaudio[ext=m4a]/{f['format_id']}+bestaudio",
                      "merge": not progressive, "convert": None, "filesize": sz,
                      "filesize_text": ("~" if est and sz else "") + fmt_bytes(sz), "estimated": est})

    # Some sources report no resolution/codec data: still offer a working choice
    if not video and not audio and formats:
        sz, est = size_of(formats[-1])
        video.append({"id": "best", "type": "video", "label": "Best available", "height": None,
                      "ext": formats[-1].get("ext") or "mp4", "codec": "", "fps": None,
                      "selector": "best", "merge": False, "convert": None, "filesize": sz,
                      "filesize_text": ("~" if est and sz else "") + fmt_bytes(sz), "estimated": est})

    return {"title": info.get("title") or "Untitled", "uploader": info.get("uploader"),
            "platform": info.get("extractor_key"), "thumbnail": info.get("thumbnail"),
            "duration": duration, "duration_text": fmt_duration(duration),
            "ffmpeg": ffmpeg, "video": video, "audio": audio}


def inspect_media(url):
    """Query the source with yt-dlp (NO download) and return the options JSON."""
    opts = {**base_opts(), "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next(e for e in info["entries"] if e)
    data = parse_info(info, ffmpeg=bool(ffmpeg_path()))
    for o in data["video"] + data["audio"]:
        o["engine"] = "yt-dlp"
    return {"route": "media", **data}


class MediaTask(BaseTask):
    kind = "media"
    engine_name = "yt-dlp"

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._base = 0              # bytes of already-finished streams (video, then audio)
        self._last_save = 0.0

    def _cleanup(self):
        if self.status == "Completed":
            return
        for p in self.meta.get("tmp", []):
            for f in (p, p + ".ytdl"):
                if os.path.exists(f):
                    os.remove(f)

    # ---------- yt-dlp hooks (progress / status callbacks) ----------
    def _hook(self, d, ev):
        if ev.is_set():
            raise DownloadCancelled("stopped by user")   # pause / cancel
        if d.get("tmpfilename") and d["tmpfilename"] not in self.meta.setdefault("tmp", []):
            self.meta["tmp"].append(d["tmpfilename"])
        if d["status"] == "downloading":
            cur = d.get("downloaded_bytes") or 0
            stream_total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            self.downloaded = self._base + cur
            self.total_size = max(self.total_size, self._base + stream_total, self.downloaded)
            self.speed = d.get("speed") or 0.0
            self.eta = fmt_eta(d["eta"]) if d.get("eta") is not None else "..."
            if time.time() - self._last_save >= 1:
                self._last_save = time.time()
                self.save()
        elif d["status"] == "finished":
            self._base += d.get("total_bytes") or d.get("downloaded_bytes") or 0

    def _pp_hook(self, d):
        if d["status"] == "started" and not self.stop.is_set():
            self.status, self.speed, self.eta = "Processing", 0.0, "merging/converting"
            self.save()

    def _execute(self, ev):
        opt = self.meta["option"]
        template = self.meta.get("template") or "%(title).150s"
        self._base = 0
        ydl_opts = {
            **base_opts(), "noprogress": True,
            "format": opt["selector"],
            "outtmpl": os.path.join(self.save_dir, template + ".%(ext)s"),
            "progress_hooks": [lambda d: self._hook(d, ev)],
            "postprocessor_hooks": [self._pp_hook],
            "post_hooks": [lambda path: self.meta.__setitem__("final", path)],
            "continuedl": True, "retries": 5, "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,      # parallel DASH/HLS fragments
        }
        if opt.get("merge"):
            ydl_opts["merge_output_format"] = "mp4"
        if opt.get("convert"):
            ydl_opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                           "preferredcodec": opt["convert"]["codec"],
                                           "preferredquality": opt["convert"]["quality"]}]
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([self.url])
        except DownloadCancelled:
            return
        except Exception:
            if ev.is_set():
                return
            raise
        if ev.is_set():
            return
        final = self.meta.get("final")
        if final and os.path.exists(final):
            self.save_path, self.filename = final, os.path.basename(final)
            self.total_size = self.downloaded = os.path.getsize(final)
        self.meta.pop("tmp", None)
        self._complete(ev)
