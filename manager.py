"""DownloadManager - the central router that unifies every engine.

    url -> classify() -> "direct"  -> DirectTask (multi-threaded Range engine)
                       -> "media"   -> first engine that can read it:
                                       yt-dlp -> pytube (YouTube) -> you-get -> lux -> gallery-dl
                       -> "gallery" -> gallery-dl -> yt-dlp

UI hook-up: `manager.add_listener(fn)`; fn(task_dict) fires on every progress /
status change (id, status, percent, speed, eta, downloaded, total_size, engine).
"""
import json
import os
import queue
import shutil
import threading
import time

import cli_engines
import database as db
import network
import pytube_engine
import settings
from cli_engines import GalleryTask, LuxTask, YouGetTask
from direct_engine import DirectTask
from media_engine import MediaTask, inspect_media
from pytube_engine import PytubeTask
from router import classify, is_youtube
from utils import (clean_filename, fmt_bytes, is_media_allowed, pick_filename,
                   strip_ansi, unique_path)

# engine name -> function that lists formats WITHOUT downloading
INSPECTORS = {
    "yt-dlp": inspect_media,
    "pytube": pytube_engine.inspect_pytube,
    "you-get": cli_engines.inspect_youget,
    "lux": cli_engines.inspect_lux,
    "gallery-dl": cli_engines.inspect_gallery,
}
TASK_CLASSES = {"yt-dlp": MediaTask, "pytube": PytubeTask, "you-get": YouGetTask,
                "lux": LuxTask, "gallery-dl": GalleryTask}
ACTIVE = ("Queued", "Downloading", "Processing")


def engine_status():
    return {"yt-dlp": True, "multi-threaded": True,
            "pytube": pytube_engine.available(),
            "you-get": cli_engines.available("you-get"),
            "lux": cli_engines.available("lux"),
            "gallery-dl": cli_engines.available("gallery-dl")}


class DownloadManager:
    def __init__(self, download_dir):
        self.download_dir = download_dir
        self.tasks, self.listeners = {}, []
        self.q = queue.Queue()
        self._cache = {}                 # url -> (timestamp, inspect result)
        self._last_id = 0
        self.limit, self.running, self.cv = settings.get("max_concurrent"), 0, threading.Condition()
        db.init_db()
        for row in db.load_all():        # restore tasks from the previous session
            status = "Paused" if row["status"] in ACTIVE else row["status"]
            meta = json.loads(row["meta"] or "{}")
            cls = (DirectTask if row.get("kind") in (None, "direct") else
                   TASK_CLASSES.get(meta.get("engine") or "yt-dlp", MediaTask))
            self.tasks[row["id"]] = cls(
                row["id"], row["url"], row["save_dir"], save_path=row["save_path"],
                total_size=row["total_size"] or 0, downloaded=row["downloaded"] or 0,
                status=status, meta=meta, listeners=self.listeners)
        for _ in range(8):               # worker pool; `limit` decides how many may run at once
            threading.Thread(target=self._worker, daemon=True).start()

    def add_listener(self, fn):
        self.listeners.append(fn)

    # ---------- helpers ----------
    def _new_id(self):
        self._last_id = max(self._last_id + 1, int(time.time() * 1000))
        return str(self._last_id)

    def inside(self, path):
        """True if `path` is inside the Downloads folder (guards open/preview/delete)."""
        root, p = os.path.realpath(self.download_dir), os.path.realpath(path)
        return p != root and p.startswith(root + os.sep)

    def disk(self):
        p = self.download_dir
        while not os.path.exists(p) and os.path.dirname(p) != p:
            p = os.path.dirname(p)
        u = shutil.disk_usage(p)
        return {"path": self.download_dir, "free": u.free, "total": u.total,
                "used_percent": round(u.used * 100 / u.total, 1),
                "free_text": fmt_bytes(u.free), "total_text": fmt_bytes(u.total)}

    def set_limit(self, n):
        with self.cv:
            self.limit = max(1, int(n))
            self.cv.notify_all()

    # ---------- inspection (nothing is downloaded) ----------
    def _chain(self, route, url):
        if route == "gallery":
            order = ["gallery-dl", "yt-dlp"]
        else:
            order = ["yt-dlp"] + (["pytube"] if is_youtube(url) else []) + ["you-get", "lux", "gallery-dl"]
        status = engine_status()
        return [e for e in order if status.get(e)]

    def _inspect_media_chain(self, route, url):
        """Try each engine in order; the first that returns usable formats wins."""
        failures = []
        for engine in self._chain(route, url):
            try:
                data = INSPECTORS[engine](url)
                if not (data.get("video") or data.get("audio") or data.get("options")):
                    raise ValueError("no formats")
                for o in data.get("video", []) + data.get("audio", []):
                    o.setdefault("engine", engine)
                note = (f"{failures[0][0]} couldn't read this link, so {engine} was used instead."
                        if failures else None)
                return {"engine": engine, "note": note, "tried": [f[0] for f in failures], **data}
            except Exception as e:
                failures.append((engine, strip_ansi(str(e)).split("ERROR:")[-1].strip()[:140]))
        raise ValueError("No engine could extract this link. " +
                         " | ".join(f"{n}: {m}" for n, m in failures))

    def inspect(self, url):
        """Classify the link and return everything the Add-Download dialog needs."""
        net_note = network.preflight(url)          # fails fast with a clear message if unreachable
        route, probe = classify(url)
        if route == "direct":
            if settings.get("restrict_media") and not is_media_allowed(probe):
                raise ValueError("Strict mode is on: only images (PNG, JPG), audio (MP3) and video (MP4) "
                                 "are allowed. Turn this off in Settings to download other file types.")
            name = pick_filename(probe)
            result = {"route": "direct", "engine": "multi-threaded", "title": name, "filename": name,
                      "filesize": probe["total_size"], "filesize_text": fmt_bytes(probe["total_size"]),
                      "resumable": probe["ranged"], "content_type": probe["content_type"], "_probe": probe}
        else:
            result = self._inspect_media_chain(route, url)
        result.update(default_save_path=self.download_dir, network_note=net_note, disk=self.disk())
        self._cache[url] = (time.time(), result)
        return {k: v for k, v in result.items() if k != "_probe"}

    def _cached(self, url):
        hit = self._cache.get(url)
        if not (hit and time.time() - hit[0] < 600):
            self.inspect(url)
            hit = self._cache[url]
        return hit[1]

    # ---------- creating tasks ----------
    def _unique_stem(self, stem):
        """A filename stem that doesn't collide with anything already in Downloads."""
        stem = clean_filename(stem) or "media"
        existing = os.listdir(self.download_dir) if os.path.isdir(self.download_dir) else []
        cand, n = stem, 1
        while any(f.startswith(cand + ".") for f in existing):
            cand, n = f"{stem} ({n})", n + 1
        return cand

    def add(self, url, option_id=None, name=None, threads=None, compat=None):
        info = self._cached(url)
        tid, user = self._new_id(), clean_filename(name or "")
        threads = max(1, min(int(threads or settings.get("threads")), 16))
        compat = settings.get("phone_friendly") if compat is None else bool(compat)

        if info["route"] == "direct":
            path = unique_path(self.download_dir, pick_filename(info["_probe"], name))
            task = DirectTask(tid, url, self.download_dir, save_path=path,
                              meta={"threads": threads}, listeners=self.listeners)

        elif info["route"] == "gallery":
            folder = unique_path(self.download_dir, user or clean_filename(info["title"]) or "gallery")
            task = GalleryTask(tid, url, self.download_dir, save_path=folder, meta={
                "engine": "gallery-dl", "count": info["count"], "thumbnail": (info.get("previews") or [None])[0],
                "option": info["options"][0], "detail": f"0 / {info['count']} files"}, listeners=self.listeners)

        else:
            options = info["video"] + info["audio"]
            opt = next((o for o in options if o["id"] == option_id), options[0])
            engine = opt.get("engine") or info["engine"]
            out_name = self._unique_stem(user or info["title"])
            meta = {"option": opt, "engine": engine, "title": info["title"], "thumbnail": info.get("thumbnail"),
                    "out_name": out_name, "template": out_name.replace("%", "%%"),
                    "compat": compat and opt.get("type") == "video",
                    "duration_text": info.get("duration_text") or None,
                    "resolution": f"{opt['height']}p" if opt.get("height") else None}
            task = TASK_CLASSES[engine](tid, url, self.download_dir, f"{out_name}.{opt.get('ext') or 'mp4'}",
                                        total_size=opt.get("filesize") or 0, meta=meta, listeners=self.listeners)
        self.tasks[tid] = task
        self.q.put(task)
        return task

    def redownload(self, task_id):
        """Start a fresh copy of an existing download (same link and quality)."""
        t = self.tasks.get(task_id)
        if not t:
            raise ValueError("Download not found")
        tid = self._new_id()
        keep = {k: v for k, v in t.meta.items() if k not in ("tmp", "final", "error", "note", "pct", "detail", "chunks")}
        if t.kind == "direct":
            new = DirectTask(tid, t.url, self.download_dir, save_path=unique_path(self.download_dir, t.filename),
                             meta=keep, listeners=self.listeners)
        elif t.kind == "gallery":
            new = GalleryTask(tid, t.url, self.download_dir, save_path=unique_path(self.download_dir, t.filename),
                              meta={**keep, "detail": f"0 / {keep.get('count', 0)} files"}, listeners=self.listeners)
        else:
            stem = self._unique_stem(os.path.splitext(t.filename)[0])
            keep.update(out_name=stem, template=stem.replace("%", "%%"))
            ext = (keep.get("option") or {}).get("ext") or "mp4"
            new = TASK_CLASSES[keep.get("engine") or "yt-dlp"](
                tid, t.url, self.download_dir, f"{stem}.{ext}", total_size=(keep.get("option") or {}).get("filesize") or 0,
                meta=keep, listeners=self.listeners)
        self.tasks[tid] = new
        self.q.put(new)
        return new

    def _worker(self):
        while True:
            task = self.q.get()
            with self.cv:
                while self.running >= self.limit:
                    self.cv.wait()
                self.running += 1
            try:
                if task.status == "Queued" and not task.cancelled:
                    task.run()
            finally:
                with self.cv:
                    self.running -= 1
                    self.cv.notify_all()

    # ---------- controls ----------
    def pause(self, task_id):
        if t := self.tasks.get(task_id):
            t.pause()

    def resume(self, task_id):
        if (t := self.tasks.get(task_id)) and t.resume():
            self.q.put(t)

    def pause_all(self):
        for t in list(self.tasks.values()):
            t.pause()

    def resume_all(self):
        for tid, t in list(self.tasks.items()):
            if t.status == "Paused":
                self.resume(tid)

    def cancel(self, task_id):
        """Remove from the list (partial files are cleaned up; finished files are kept)."""
        if t := self.tasks.pop(task_id, None):
            t.cancel()
            db.delete(task_id)

    def delete(self, task_id, delete_file=False):
        """Remove from the list and (optionally) delete the finished file/folder from disk."""
        t = self.tasks.get(task_id)
        if not t:
            return
        path, was_done = t.save_path, t.status == "Completed"
        self.cancel(task_id)
        if delete_file and was_done and self.inside(path):
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)

    def clear_finished(self):
        for tid, t in list(self.tasks.items()):
            if t.status == "Completed":
                self.cancel(tid)

    def rename(self, task_id, new_name):
        if not (t := self.tasks.get(task_id)):
            raise ValueError("Download not found")
        return t.rename(new_name)

    def list(self):
        return [t.to_dict() for t in self.tasks.values()]
