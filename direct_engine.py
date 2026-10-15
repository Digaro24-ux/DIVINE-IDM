"""Engine 2 - Traditional multi-threaded downloader (HTTP Range chunks).

* The destination file is PRE-ALLOCATED to its full size, then every thread
  writes its own byte range straight into it (no merge step needed).
* Per-chunk progress is saved in SQLite, so Pause/Resume (even after an app
  restart) continues each chunk exactly where it stopped.
"""
import os
import shutil
import threading
import time

import requests

from base import BaseTask
from utils import HEADERS, fmt_eta, probe_url

READ_SIZE = 64 * 1024


def preallocate(path, size):
    """Reserve `size` bytes on disk (real allocation where the OS supports it)."""
    with open(path, "wb") as f:
        try:
            os.posix_fallocate(f.fileno(), 0, size)
        except (AttributeError, OSError):
            f.truncate(size)          # Windows/macOS: sparse pre-sized file


class DirectTask(BaseTask):
    kind = "direct"
    engine_name = "multi-threaded"

    def __init__(self, *args, threads=8, **kw):
        super().__init__(*args, **kw)
        self.threads = self.meta.get("threads", threads)
        self.meta["threads"] = self.threads
        self.final_url = self.url
        self.ranged = False
        self.chunks = []                 # bytes written per chunk

    @property
    def tmp_path(self):
        return self.save_path + ".idm"

    def _cleanup(self):
        if self.status != "Completed" and os.path.exists(self.tmp_path):
            os.remove(self.tmp_path)

    def _execute(self, ev):
        info = probe_url(self.url)
        self.final_url, self.total_size, self.ranged = info["final_url"], info["total_size"], info["ranged"]
        self.save()
        self._multi(ev) if self.ranged else self._single(ev)

    # ---------- multi-threaded path ----------
    def _multi(self, ev):
        total, tmp = self.total_size, self.tmp_path
        n = max(1, min(self.threads, total // (1 << 20) or 1))
        size = total // n
        ranges = [(i * size, total - 1 if i == n - 1 else (i + 1) * size - 1) for i in range(n)]

        saved = self.meta.get("chunks")
        resumable = (saved and len(saved) == n and os.path.exists(tmp)
                     and os.path.getsize(tmp) == total)
        if resumable:
            self.chunks = [min(c, e - s + 1) for c, (s, e) in zip(saved, ranges)]
        else:
            if shutil.disk_usage(self.save_dir).free < total:
                raise RuntimeError("Not enough free disk space")
            preallocate(tmp, total)
            self.chunks = [0] * n
        self.downloaded = sum(self.chunks)

        errors = []
        workers = [threading.Thread(target=self._fetch, args=(ev, i, s, e, errors), daemon=True)
                   for i, (s, e) in enumerate(ranges)]
        for t in workers:
            t.start()

        last_bytes, last_t = self.downloaded, time.time()
        while any(t.is_alive() for t in workers):
            time.sleep(1)
            now, cur = time.time(), self.downloaded
            speed = (cur - last_bytes) / max(now - last_t, 1e-6)
            last_bytes, last_t = cur, now
            if not ev.is_set():
                self.speed = speed
                self.eta = fmt_eta((total - cur) / speed) if speed > 0 else "..."
                self.meta["chunks"] = list(self.chunks)   # persist resume point
                self.save()

        self.meta["chunks"] = list(self.chunks)
        if ev.is_set():
            return                                        # paused / cancelled
        if errors or any(c != e - s + 1 for c, (s, e) in zip(self.chunks, ranges)):
            self.save()
            raise RuntimeError(errors[0] if errors else "Connection dropped - press Resume")

        os.replace(tmp, self.save_path)
        self.downloaded = total
        self.meta.pop("chunks", None)
        self._set("Completed")

    def _fetch(self, ev, i, start, end, errors):
        """Download one chunk; retries up to 3 times continuing from its offset."""
        for attempt in range(3):
            pos = start + self.chunks[i]
            if pos > end or ev.is_set():
                return
            try:
                r = requests.get(self.final_url, stream=True, timeout=20,
                                 headers={**HEADERS, "Range": f"bytes={pos}-{end}"})
                if r.status_code != 206:
                    raise RuntimeError(f"Server answered HTTP {r.status_code} to a range request")
                with open(self.tmp_path, "r+b") as f:
                    f.seek(pos)
                    for data in r.iter_content(READ_SIZE):
                        if ev.is_set():
                            return
                        f.write(data)
                        with self.lock:
                            self.chunks[i] += len(data)
                            self.downloaded += len(data)
            except Exception as e:
                if attempt == 2:
                    errors.append(str(e))
                time.sleep(1)

    # ---------- fallback: no range support (cannot resume) ----------
    def _single(self, ev):
        tmp, last_bytes, last_t = self.tmp_path, 0, time.time()
        self.downloaded = 0
        with requests.get(self.final_url, stream=True, timeout=20, headers=HEADERS) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for data in r.iter_content(READ_SIZE):
                    if ev.is_set():
                        return
                    f.write(data)
                    self.downloaded += len(data)
                    now = time.time()
                    if now - last_t >= 1:
                        self.speed = (self.downloaded - last_bytes) / (now - last_t)
                        last_bytes, last_t = self.downloaded, now
                        if self.total_size and self.speed > 0:
                            self.eta = fmt_eta((self.total_size - self.downloaded) / self.speed)
                        self.save()
        os.replace(tmp, self.save_path)
        self._set("Completed")
