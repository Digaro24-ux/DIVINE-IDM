#!/usr/bin/env python3
"""
GhostDL - a small, dependency-free download manager inspired by Ghost Downloader 3.

Features
  * Multi-connection (segmented) HTTP/HTTPS downloads via Range requests
  * Pause / resume (progress is saved to a .ghostdl sidecar file, survives restarts)
  * FTP support (single stream, resumable via REST)
  * Automatic retries, speed + ETA display
  * CLI and a simple Tk GUI (python ghostdl.py --gui)

Not included: torrents, YouTube/Bilibili extraction, browser extension.
Those need heavy third-party pieces (libtorrent, yt-dlp, a browser extension).

Usage
  python ghostdl.py URL [URL ...] [-o DIR] [-n CONNECTIONS]
  python ghostdl.py --gui
"""
import argparse
import ftplib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

UA = "GhostDL/1.0"
CHUNK = 64 * 1024
MIN_SEGMENT = 512 * 1024  # don't split files into pieces smaller than this
RETRIES = 6
TIMEOUT = 20


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def filename_from(url, headers=None):
    if headers:
        cd = headers.get("Content-Disposition")
        if cd:
            m = Message()
            m["content-disposition"] = cd
            name = m.get_param("filename", header="content-disposition")
            if isinstance(name, tuple):
                name = name[2]
            if name:
                return os.path.basename(urllib.parse.unquote(name))
            m2 = re.search(r"filename\*=[^']*'[^']*'([^;]+)", cd)
            if m2:
                return os.path.basename(urllib.parse.unquote(m2.group(1)))
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    return os.path.basename(path) or "download.bin"


def unique_path(path):
    if not os.path.exists(path) or os.path.exists(path + ".ghostdl"):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


class Download:
    """One download job. States: queued, running, paused, done, error."""

    def __init__(self, url, out_dir=".", connections=8, filename=None):
        self.url = url
        self.out_dir = out_dir
        self.connections = max(1, connections)
        self.filename = filename
        self.path = None
        self.total = 0
        self.segments = []  # [start, end_inclusive, downloaded]
        self.state = "queued"
        self.error = None
        self.scheme = urllib.parse.urlparse(url).scheme.lower()
        self._pause = threading.Event()
        self._lock = threading.Lock()
        self._threads = []
        self._speed_samples = []
        self.speed = 0.0
        self.accept_ranges = False

    # ---- public properties -------------------------------------------------
    @property
    def downloaded(self):
        return sum(s[2] for s in self.segments)

    @property
    def percent(self):
        return 100.0 * self.downloaded / self.total if self.total else 0.0

    @property
    def eta(self):
        if self.speed > 0 and self.total:
            return (self.total - self.downloaded) / self.speed
        return None

    # ---- control -----------------------------------------------------------
    def start(self):
        if self.state == "running":
            return
        self._pause.clear()
        self.state = "running"
        self.error = None
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        self._main = t

    def pause(self):
        if self.state == "running":
            self._pause.set()

    def join(self):
        t = getattr(self, "_main", None)
        if t:
            t.join()

    # ---- internals ---------------------------------------------------------
    def _request(self, headers=None, method="GET"):
        h = {"User-Agent": UA, "Accept-Encoding": "identity"}
        if headers:
            h.update(headers)
        req = urllib.request.Request(self.url, headers=h, method=method)
        return urllib.request.urlopen(req, timeout=TIMEOUT)

    def _probe(self):
        """Find size, range support and filename."""
        if self.scheme in ("http", "https"):
            try:
                resp = self._request({"Range": "bytes=0-0"})
            except urllib.error.HTTPError as e:
                if e.code == 416:  # empty file
                    self.total, self.accept_ranges = 0, False
                    self.filename = self.filename or filename_from(self.url, e.headers)
                    return
                raise
            with resp:
                hdrs = resp.headers
                cr = hdrs.get("Content-Range")
                if resp.status == 206 and cr and "/" in cr and cr.split("/")[-1].isdigit():
                    self.total = int(cr.split("/")[-1])
                    self.accept_ranges = True
                else:
                    self.total = int(hdrs.get("Content-Length") or 0)
                    self.accept_ranges = False
                self.filename = self.filename or filename_from(resp.geturl(), hdrs)
        elif self.scheme == "ftp":
            ftp = self._ftp_connect()
            try:
                ftp.voidcmd("TYPE I")
                self.total = ftp.size(self._ftp_path()) or 0
            finally:
                try:
                    ftp.quit()
                except Exception:
                    pass
            self.accept_ranges = False
            self.filename = self.filename or filename_from(self.url)
        else:
            raise ValueError(f"Unsupported scheme: {self.scheme!r}")

    def _plan_segments(self):
        if not self.accept_ranges or self.total < MIN_SEGMENT * 2:
            n = 1
        else:
            n = min(self.connections, max(1, self.total // MIN_SEGMENT))
        if self.total == 0:
            self.segments = [[0, -1, 0]]
            return
        size = self.total // n
        self.segments = []
        for i in range(n):
            start = i * size
            end = self.total - 1 if i == n - 1 else start + size - 1
            self.segments.append([start, end, 0])

    def _state_file(self):
        return self.path + ".ghostdl"

    def _save_state(self):
        data = {
            "url": self.url,
            "total": self.total,
            "accept_ranges": self.accept_ranges,
            "segments": self.segments,
        }
        tmp = self._state_file() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self._state_file())

    def _load_state(self):
        try:
            with open(self._state_file()) as f:
                data = json.load(f)
            if data["url"] == self.url and os.path.exists(self.path):
                self.total = data["total"]
                self.accept_ranges = data["accept_ranges"]
                self.segments = data["segments"]
                return True
        except (OSError, ValueError, KeyError):
            pass
        return False

    def _run(self):
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            if not self.segments:
                self._probe()
                self.path = unique_path(os.path.join(self.out_dir, self.filename))
                if not self._load_state():
                    self._plan_segments()
                    with open(self.path, "wb") as f:
                        if self.total:
                            f.truncate(self.total)
            self._threads = []
            self._speed_samples = [(time.time(), self.downloaded)]
            stop = threading.Event()
            mon = threading.Thread(target=self._monitor, args=(stop,), daemon=True)
            mon.start()
            errors = []
            for idx in range(len(self.segments)):
                seg = self.segments[idx]
                if seg[1] >= 0 and seg[0] + seg[2] > seg[1]:
                    continue  # segment already finished
                t = threading.Thread(target=self._worker, args=(idx, errors), daemon=True)
                t.start()
                self._threads.append(t)
            for t in self._threads:
                t.join()
            stop.set()
            mon.join()
            self._save_state()
            if errors:
                raise errors[0]
            if self._pause.is_set():
                self.state = "paused"
                return
            self.speed = 0
            if self.total == 0:
                self.total = self.segments[0][2]
            os.remove(self._state_file())
            self.state = "done"
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self.state = "error"

    def _monitor(self, stop):
        last_save = time.time()
        while not stop.wait(0.5):
            now = time.time()
            self._speed_samples.append((now, self.downloaded))
            self._speed_samples = [s for s in self._speed_samples if now - s[0] <= 4]
            t0, b0 = self._speed_samples[0]
            self.speed = (self.downloaded - b0) / (now - t0) if now > t0 else 0
            if now - last_save > 2:
                try:
                    self._save_state()
                except OSError:
                    pass
                last_save = now

    def _worker(self, idx, errors):
        seg = self.segments[idx]
        attempt = 0
        while not self._pause.is_set():
            try:
                if self.scheme == "ftp":
                    self._worker_ftp(seg)
                else:
                    self._worker_http(seg)
                return
            except (OSError, urllib.error.URLError, ftplib.Error, socket.timeout) as e:
                if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 404, 410):
                    errors.append(e)
                    self._pause.set()
                    return
                attempt += 1
                if attempt > RETRIES:
                    errors.append(e)
                    self._pause.set()
                    return
                time.sleep(min(2 ** attempt, 15))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                self._pause.set()
                return

    def _worker_http(self, seg):
        start, end, done = seg
        headers = {}
        if self.accept_ranges and end >= 0:
            if start + done > end:
                return
            headers["Range"] = f"bytes={start + done}-{end}"
        elif done:
            seg[2] = 0  # server can't resume: start over
            with self._lock:
                pass
        with self._request(headers) as resp:
            if headers and resp.status != 206:
                raise OSError("Server ignored Range request")
            with open(self.path, "r+b") as f:
                f.seek(start + seg[2])
                while not self._pause.is_set():
                    buf = resp.read(CHUNK)
                    if not buf:
                        break
                    if end >= 0:
                        remaining = end - (start + seg[2]) + 1
                        buf = buf[:remaining]
                    f.write(buf)
                    seg[2] += len(buf)
                    if end >= 0 and start + seg[2] > end:
                        break
        if end >= 0 and not self._pause.is_set() and start + seg[2] <= end:
            raise OSError("Connection closed early")

    def _ftp_connect(self):
        u = urllib.parse.urlparse(self.url)
        ftp = ftplib.FTP()
        ftp.connect(u.hostname, u.port or 21, timeout=TIMEOUT)
        ftp.login(urllib.parse.unquote(u.username or "anonymous"),
                  urllib.parse.unquote(u.password or "ghostdl@example.com"))
        return ftp

    def _ftp_path(self):
        return urllib.parse.unquote(urllib.parse.urlparse(self.url).path)

    def _worker_ftp(self, seg):
        ftp = self._ftp_connect()
        try:
            ftp.voidcmd("TYPE I")
            with open(self.path, "r+b") as f:
                f.seek(seg[2])

                def cb(data):
                    if self._pause.is_set():
                        raise _Paused()
                    f.write(data)
                    seg[2] += len(data)

                try:
                    ftp.retrbinary(f"RETR {self._ftp_path()}", cb, CHUNK,
                                   rest=seg[2] or None)
                except _Paused:
                    pass
        finally:
            try:
                ftp.close()
            except Exception:
                pass


class _Paused(Exception):
    pass


