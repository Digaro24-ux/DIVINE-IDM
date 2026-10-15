"""Engine 3 - PyTube (YouTube-only fallback).

Original `pytube` is no longer maintained and breaks often, so this uses the
actively maintained, API-compatible fork `pytubefix` (falls back to plain
`pytube` if that's what is installed). Offers progressive MP4 (video+audio in
one file, up to 720p) and audio-only streams. It cannot resume: Pause stops,
Resume restarts the file.
"""
import os
import time

try:
    from pytubefix import YouTube
except ImportError:                      # pragma: no cover
    try:
        from pytube import YouTube
    except ImportError:
        YouTube = None

from base import BaseTask
from utils import fmt_bytes, fmt_duration, fmt_eta


class _Stop(Exception):
    pass


def available():
    return YouTube is not None


def _size(stream):
    try:
        return int(stream.filesize_approx)
    except Exception:
        return None


def _opt(stream, kind):
    sz = _size(stream)
    if kind == "video":
        res = stream.resolution or "?"
        label, ext = res + (" HD" if res in ("720p", "1080p") else ""), "mp4"
    else:
        abr = (getattr(stream, "abr", "") or "").replace("kbps", " kbps")
        ext = "m4a" if "mp4" in (stream.mime_type or "") else "webm"
        label = f"{ext.upper()} {abr}".strip()
    digits = "".join(ch for ch in ((stream.resolution if kind == "video" else getattr(stream, "abr", "")) or "") if ch.isdigit())
    return {"height": int(digits) if kind == "video" and digits else None,
            "abr": int(digits) if kind == "audio" and digits else None,
            "id": f"pytube-{stream.itag}", "type": kind, "label": label, "ext": ext,
            "codec": (getattr(stream, "video_codec" if kind == "video" else "audio_codec", "") or "").split(".")[0],
            "selector": str(stream.itag), "merge": False, "convert": None, "engine": "pytube",
            "filesize": sz, "filesize_text": "~" + fmt_bytes(sz) if sz else "Unknown", "estimated": True}


def inspect_pytube(url):
    if YouTube is None:
        raise ValueError("pytube/pytubefix is not installed")
    yt = YouTube(url)
    video = [_opt(s, "video") for s in yt.streams.filter(progressive=True).order_by("resolution").desc()]
    audio = [_opt(s, "audio") for s in yt.streams.filter(only_audio=True).order_by("abr").desc()]
    length = yt.length or 0
    return {"route": "media", "title": yt.title, "uploader": yt.author, "platform": "YouTube (pytube)",
            "thumbnail": yt.thumbnail_url, "duration": length, "duration_text": fmt_duration(length),
            "ffmpeg": False, "video": video, "audio": audio}


class PytubeTask(BaseTask):
    kind = "media"
    engine_name = "pytube"

    def _cleanup(self):
        if self.status != "Completed" and os.path.exists(self.save_path):
            os.remove(self.save_path)

    def _execute(self, ev):
        opt = self.meta["option"]
        state = {"t": time.time(), "b": 0, "saved": 0.0}

        def progress(stream, chunk, remaining):
            if ev.is_set():
                raise _Stop()                       # pause / cancel
            self.total_size = stream.filesize
            self.downloaded = self.total_size - remaining
            now = time.time()
            if now - state["t"] >= 1:
                self.speed = (self.downloaded - state["b"]) / (now - state["t"])
                state["t"], state["b"] = now, self.downloaded
                self.eta = fmt_eta(remaining / self.speed) if self.speed > 0 else "..."
                self.save()

        try:
            yt = YouTube(self.url, on_progress_callback=progress)
            stream = yt.streams.get_by_itag(int(opt["selector"]))
            self.downloaded = 0
            stream.download(output_path=self.save_dir, filename=os.path.basename(self.save_path))
        except _Stop:
            return
        if ev.is_set():
            return
        self.total_size = self.downloaded = os.path.getsize(self.save_path)
        self._set("Completed")
