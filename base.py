"""Shared task behaviour (state, persistence, pause/resume/cancel/rename)."""
import json
import os
import threading
import time

import database as db
from utils import PREVIEW_EXTS, category_of, clean_filename


class BaseTask:
    kind = "direct"
    engine_name = ""

    def __init__(self, task_id, url, save_dir, filename=None, save_path=None,
                 total_size=0, downloaded=0, status="Queued", meta=None, listeners=None):
        self.id, self.url, self.save_dir = task_id, url, save_dir
        self.save_path = save_path or os.path.join(save_dir, filename)
        self.filename = os.path.basename(self.save_path)
        self.total_size, self.downloaded, self.status = total_size, downloaded, status
        self.speed, self.eta = 0.0, "-"
        self.meta = meta or {}
        self.listeners = listeners if listeners is not None else []
        self.stop = threading.Event()
        self.cancelled = False
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()   # held while a run is active
        self.save()

    # ---------- state / persistence / UI hooks ----------
    def to_dict(self):
        ext = os.path.splitext(self.filename)[1].lstrip(".").lower() if self.kind != "gallery" else ""
        try:
            added = int(self.id) / 1000
        except ValueError:
            added = 0
        if self.status == "Completed":
            pct = 100.0
        elif "pct" in self.meta:          # engines that report their own percentage
            pct = round(self.meta["pct"], 1)
        else:
            pct = round(self.downloaded * 100 / self.total_size, 1) if self.total_size else 0.0
        return {"id": self.id, "url": self.url, "kind": self.kind,
                "filename": self.filename, "save_path": self.save_path,
                "total_size": self.total_size, "downloaded": self.downloaded,
                "percent": pct, "status": self.status, "speed": self.speed,
                "eta": self.eta, "engine": self.engine_name,
                "detail": self.meta.get("detail"), "error": self.meta.get("error"), "note": self.meta.get("note"),
                "thumbnail": self.meta.get("thumbnail"), "category": category_of(self.filename, self.kind),
                "format": ext.upper(), "duration": self.meta.get("duration_text"),
                "resolution": self.meta.get("resolution"), "threads": self.meta.get("threads"),
                "added": added, "can_preview": self.status == "Completed" and ext in PREVIEW_EXTS,
                "shareable": self.status == "Completed" and self.kind != "gallery",
                "quality": (self.meta.get("option") or {}).get("label")}

    def save(self):
        """Persist to SQLite and notify listeners (progress/status hooks)."""
        if self.cancelled:
            return
        row = {**self.to_dict(), "save_dir": self.save_dir, "meta": json.dumps(self.meta)}
        db.upsert(row)
        for fn in self.listeners:
            try:
                fn(self.to_dict())
            except Exception:
                pass

    def _set(self, status):
        self.status = status
        if status == "Completed":          # file date = download time, so it shows at the top of Gallery
            try:
                if os.path.isfile(self.save_path):
                    os.utime(self.save_path, None)
            except OSError:
                pass
        if status != "Downloading":
            self.speed, self.eta = 0.0, "-"
        self.save()

    # ---------- control ----------
    def pause(self):
        if self.status in ("Queued", "Downloading"):
            self.stop.set()
            self._set("Paused")

    def resume(self):
        if self.status in ("Paused", "Failed"):
            self.stop = threading.Event()   # fresh event; an old run keeps its own
            self._set("Queued")
            return True
        return False

    def cancel(self):
        was_done = self.status == "Completed"
        self.cancelled = True
        self.stop.set()
        if not was_done and self.run_lock.acquire(blocking=False):
            try:
                self._cleanup()
            finally:
                self.run_lock.release()

    def rename(self, new_name):
        if self.status != "Completed":
            raise ValueError("Only completed downloads can be renamed (set a name before downloading instead)")
        if not os.path.exists(self.save_path):
            raise ValueError("The file no longer exists on disk")
        cur_ext = os.path.splitext(self.save_path)[1]
        name = clean_filename(new_name)
        if not name:
            raise ValueError("Please enter a valid name")
        e = os.path.splitext(name)[1].lower()
        jpg = {".jpg", ".jpeg"}
        if not (e == cur_ext.lower() or {e, cur_ext.lower()} <= jpg):
            name += cur_ext                      # always keep the real extension
        new_path = os.path.join(os.path.dirname(self.save_path), name)
        if new_path != self.save_path and os.path.exists(new_path):
            raise ValueError("A file with that name already exists")
        os.rename(self.save_path, new_path)
        self.save_path, self.filename = new_path, name
        self.save()
        return name

    def _complete(self, ev):
        """Finish a media download: optionally make the video phone-compatible
        (H.264/AAC MP4), then mark Completed."""
        opt = self.meta.get("option") or {}
        if self.meta.get("compat") and opt.get("type") == "video" and os.path.isfile(self.save_path):
            from mediafix import make_phone_friendly
            self.status, self.speed, self.eta = "Processing", 0.0, "converting for phones"
            self.save()
            last = [0.0]

            def progress(p):
                self.meta["pct"], self.meta["detail"] = p, f"Converting for phones {p:.0f}%"
                if time.time() - last[0] > 1:
                    last[0] = time.time()
                    self.save()
            final, problem = make_phone_friendly(self.save_path, ev, progress)
            self.meta.pop("pct", None)
            self.meta.pop("detail", None)
            if ev.is_set():
                return
            if problem:
                self.meta["note"] = problem
            self.save_path, self.filename = final, os.path.basename(final)
        if os.path.isfile(self.save_path):
            self.total_size = self.downloaded = os.path.getsize(self.save_path)
        self._set("Completed")

    # ---------- run template ----------
    def run(self):
        with self.run_lock:
            ev = self.stop
            if ev.is_set() or self.cancelled:
                return
            self._set("Downloading")
            try:
                os.makedirs(self.save_dir, exist_ok=True)
                self._execute(ev)
            except Exception as e:
                if not ev.is_set():
                    print(f"[{self.filename}] failed: {e}")
                    self.meta["error"] = str(e)[:300]
                    self._set("Failed")
            finally:
                if self.cancelled:
                    self._cleanup()

    def _execute(self, ev):      # implemented by each engine
        raise NotImplementedError

    def _cleanup(self):          # implemented by each engine
        pass
