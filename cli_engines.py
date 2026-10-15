"""Engines driven through their command lines:

  * You-Get   - great for Bilibili, Youku, iQiyi, Weibo, etc.
  * Lux       - Go program (not a Python lib): used only if `lux` is on your PATH
  * gallery-dl- image/video GALLERIES (Pinterest, Imgur, Flickr, Pixiv, Reddit...)

Each tool is run as a subprocess; its progress output is parsed into the same
progress hooks the other engines use. Pause = stop the process; Resume = start
it again (these tools skip/continue files they already have where they can).
"""
import collections
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

from base import BaseTask
from utils import clean_filename, fmt_bytes, fmt_eta

TEMP_SUFFIXES = (".download", ".part", ".tmp", ".ytdl", ".idm")
AUDIO_EXTS = {"mp3", "m4a", "aac", "flac", "wav", "ogg", "opus", "wma"}
UNITS = {"B": 1, "KB": 1024, "KIB": 1024, "MB": 1024 ** 2, "MIB": 1024 ** 2, "GB": 1024 ** 3, "GIB": 1024 ** 3}
PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
PAIR_LUX = re.compile(r"([\d.]+)\s*([KMG]?i?B)\s*/\s*([\d.]+)\s*([KMG]?i?B)", re.I)       # 1.2 MiB / 5 MiB
PAIR_YG = re.compile(r"\(\s*([\d.]+)\s*/\s*([\d.]+)\s*([KMG]?B)\s*\)", re.I)              # ( 1.2/ 5.0MB)


def available(tool):
    if tool == "lux":
        return shutil.which("lux") is not None
    return importlib.util.find_spec({"you-get": "you_get", "gallery-dl": "gallery_dl"}[tool]) is not None


def parse_progress(line):
    """-> (percent|None, bytes_done|None, bytes_total|None) from one progress line."""
    pct = PCT.search(line)
    pct = float(pct.group(1)) if pct else None
    if m := PAIR_YG.search(line):
        u = m.group(3).upper()
        return pct, int(float(m.group(1)) * UNITS[u]), int(float(m.group(2)) * UNITS[u])
    if m := PAIR_LUX.search(line):
        try:
            return pct, int(float(m.group(1)) * UNITS[m.group(2).upper()]), int(float(m.group(3)) * UNITS[m.group(4).upper()])
        except (KeyError, ValueError):
            pass
    return pct, None, None


def _run_json(cmd, timeout=90):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    out = p.stdout.decode("utf-8", "replace")
    idx = [i for i in (out.find("{"), out.find("[")) if i >= 0]
    if p.returncode != 0 and not idx or not idx:
        err = (p.stderr.decode("utf-8", "replace") or out).strip().splitlines()
        raise ValueError(err[-1][:200] if err else "no output")
    return json.JSONDecoder().raw_decode(out[min(idx):])[0]


def _option(engine, key, label, ext, size, kind):
    return {"id": f"{engine}-{key}", "type": kind, "label": label, "ext": ext, "codec": "",
            "selector": key, "merge": False, "convert": None, "engine": engine,
            "filesize": size or None, "filesize_text": fmt_bytes(size) if size else "Unknown", "estimated": False}


def _split_streams(engine, streams):
    video, audio = [], []
    for key, s in (streams or {}).items():
        parts = s.get("parts") or [{}]
        ext = (s.get("container") or s.get("ext") or parts[0].get("ext") or "mp4").lower()
        label = s.get("video_profile") or s.get("quality") or key
        kind = "audio" if ext in AUDIO_EXTS else "video"
        (audio if kind == "audio" else video).append(_option(engine, key, str(label), ext, s.get("size"), kind))
    for lst in (video, audio):
        lst.sort(key=lambda o: o["filesize"] or 0, reverse=True)
    return video, audio


def _media_result(title, site, video, audio):
    if not (video or audio):
        raise ValueError("no downloadable streams found")
    return {"route": "media", "title": title or "Untitled", "uploader": None, "platform": site,
            "thumbnail": None, "duration": 0, "duration_text": "",
            "ffmpeg": shutil.which("ffmpeg") is not None, "video": video, "audio": audio}


def inspect_youget(url):
    d = _run_json([sys.executable, "-m", "you_get", "--json", url])
    video, audio = _split_streams("you-get", d.get("streams"))
    return _media_result(d.get("title"), d.get("site"), video, audio)


def inspect_lux(url):
    d = _run_json(["lux", "-j", url])
    d = d[0] if isinstance(d, list) and d else d
    if d.get("err"):
        raise ValueError(str(d["err"])[:200])
    video, audio = _split_streams("lux", d.get("streams"))
    return _media_result(d.get("title"), d.get("site"), video, audio)


def inspect_gallery(url, limit=500):
    """List the files of a gallery/post without downloading (gallery-dl -j)."""
    msgs = _run_json([sys.executable, "-m", "gallery_dl", "--range", f"1-{limit}", "-j", url], timeout=120)
    files = [m for m in msgs if isinstance(m, list) and m and m[0] == 3]
    meta = next((m[1] for m in msgs if isinstance(m, list) and m and m[0] == 2 and len(m) > 1), {}) or {}
    if not files:
        raise ValueError("no images or videos found")
    exts = [os.path.splitext(urlparse(f[1]).path)[1].lower() for f in files]
    videos = sum(e in (".mp4", ".webm", ".mkv", ".mov", ".gif") for e in exts)
    title = str(meta.get("title") or meta.get("user") or meta.get("username") or meta.get("gallery_id")
                or meta.get("category") or "gallery")
    n = len(files)
    return {"route": "gallery", "title": title, "platform": meta.get("category"), "count": n,
            "summary": f"{n}{'+' if n >= limit else ''} files ({n - videos} images, {videos} videos/gifs)",
            "previews": [f[1] for f, e in zip(files, exts) if e in (".jpg", ".jpeg", ".png", ".webp")][:4],
            "thumbnail": None, "video": [], "audio": [],
            "options": [{"id": "gallery-all", "type": "gallery", "label": f"All {n} files (original quality)",
                         "engine": "gallery-dl", "filesize_text": ""}]}


# ======================= tasks =======================
class CliTask(BaseTask):
    """Runs a command, turns its stdout into progress, kills it on pause/cancel."""
    kind = "media"

    def command(self):
        raise NotImplementedError

    def on_line(self, line):
        pct, cur, total = parse_progress(line)
        if total:
            self.total_size = max(self.total_size, total)
        if cur is not None:
            self.downloaded = cur
        elif pct is not None and self.total_size:
            self.downloaded = int(self.total_size * pct / 100)

    def on_tick(self):
        pass

    def _execute(self, ev):
        tail = collections.deque(maxlen=6)
        proc = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})

        def reader():                       # progress bars use \r, so split on \r and \n
            buf = b""
            while data := proc.stdout.read(4096):
                buf += data
                *lines, buf = re.split(rb"[\r\n]+", buf)
                for raw in lines:
                    if line := raw.decode("utf-8", "replace").strip():
                        tail.append(line)
                        self.on_line(line)
        t = threading.Thread(target=reader, daemon=True)
        t.start()

        last_b, last_t = self.downloaded, time.time()
        while proc.poll() is None:
            if ev.is_set():
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            time.sleep(0.25)
            if time.time() - last_t >= 1 and not ev.is_set():
                self.on_tick()
                now = time.time()
                self.speed = max(0.0, (self.downloaded - last_b) / (now - last_t))
                last_b, last_t = self.downloaded, now
                remaining = max(self.total_size - self.downloaded, 0)
                self.eta = fmt_eta(remaining / self.speed) if self.speed > 0 and self.total_size else "..."
                self.save()
        proc.wait()
        t.join(2)
        if ev.is_set():
            return
        if proc.returncode != 0:
            raise RuntimeError(" | ".join(list(tail)[-3:]) or f"exit code {proc.returncode}")
        self._finish(list(tail), ev)


class _SingleFileCli(CliTask):
    def _stem(self):
        return os.path.join(self.save_dir, self.meta["out_name"])

    def _outputs(self):
        return [p for p in glob.glob(glob.escape(self._stem()) + ".*") if not p.endswith(TEMP_SUFFIXES)]

    def _cleanup(self):
        if self.status != "Completed":
            for p in glob.glob(glob.escape(self._stem()) + "*"):
                if p.endswith(TEMP_SUFFIXES) or not os.path.isdir(p):
                    os.remove(p)

    def _finish(self, tail, ev):
        files = self._outputs()
        if not files:
            raise RuntimeError("Finished, but the output file was not found")
        final = max(files, key=os.path.getsize)
        self.save_path, self.filename = final, os.path.basename(final)
        self._complete(ev)


class YouGetTask(_SingleFileCli):
    engine_name = "you-get"

    def command(self):
        cmd = [sys.executable, "-m", "you_get", "-o", self.save_dir, "-O", self.meta["out_name"]]
        sel = self.meta["option"].get("selector")
        return cmd + ([f"--format={sel}"] if sel else []) + [self.url]


class LuxTask(_SingleFileCli):
    engine_name = "lux"

    def command(self):
        cmd = ["lux", "-o", self.save_dir, "-O", self.meta["out_name"]]
        sel = self.meta["option"].get("selector")
        return cmd + (["-f", sel] if sel else []) + [self.url]


class GalleryTask(CliTask):
    """Downloads a whole gallery into its own folder inside Downloads."""
    kind = "gallery"
    engine_name = "gallery-dl"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.done = 0

    def command(self):
        os.makedirs(self.save_path, exist_ok=True)
        return [sys.executable, "-m", "gallery_dl", "-D", self.save_path, self.url]

    def _dir_size(self):
        return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(self.save_path) for f in fs)

    def on_line(self, line):
        if line.startswith("["):            # log lines, not file paths
            return
        self.done += 1                      # a path (or "# path" = already existed)
        total = self.meta.get("count") or 0
        self.meta["detail"] = f"{self.done} / {total} files" if total else f"{self.done} files"
        if total:
            self.meta["pct"] = min(99.0, self.done * 100 / total)

    def on_tick(self):
        self.downloaded = self._dir_size()

    def _finish(self, tail, ev):
        if self.done == 0:
            raise RuntimeError("No files were downloaded")
        self.downloaded = self.total_size = self._dir_size()
        self.meta["detail"] = f"{self.done} files"
        self.meta.pop("pct", None)
        self._set("Completed")

    def _cleanup(self):
        if self.status != "Completed":
            shutil.rmtree(self.save_path, ignore_errors=True)

    def rename(self, new_name):
        if self.status != "Completed" or not os.path.isdir(self.save_path):
            raise ValueError("Only completed galleries can be renamed")
        name = clean_filename(new_name)
        if not name:
            raise ValueError("Please enter a valid name")
        new_path = os.path.join(os.path.dirname(self.save_path), name)
        if new_path != self.save_path and os.path.exists(new_path):
            raise ValueError("A folder with that name already exists")
        os.rename(self.save_path, new_path)
        self.save_path, self.filename = new_path, name
        self.save()
        return name
