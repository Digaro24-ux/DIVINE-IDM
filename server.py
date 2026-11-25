#!/usr/bin/env python3
"""
DIVINE IDM - web-based download manager.

    python server.py                 # http://127.0.0.1:6277
    python server.py --host 0.0.0.0  # reachable from your phone / LAN (token required)

Everything is stdlib except two optional backends:
    yt-dlp      -> YouTube, Bilibili and 1000+ sites
    libtorrent  -> torrents and magnet links
"""
import argparse
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine  # noqa: E402
import media  # noqa: E402
import torrent  # noqa: E402

CONF_DIR = os.environ.get("DIVINE_IDM_HOME", os.path.join(os.path.expanduser("~"), ".divine_idm"))
CONF_FILE = os.path.join(CONF_DIR, "config.json")
JOBS_FILE = os.path.join(CONF_DIR, "jobs.json")
STATIC = os.path.join(HERE, "static")
DEFAULT_PORT = 6277

_conf_lock = threading.Lock()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def load_config():
    os.makedirs(CONF_DIR, exist_ok=True)
    conf = {}
    try:
        with open(CONF_FILE) as f:
            conf = json.load(f)
    except (OSError, ValueError):
        pass
    conf.setdefault("token", secrets.token_urlsafe(24))
    conf.setdefault("download_dir", os.path.join(os.path.expanduser("~"), "Downloads", "DivineIDM"))
    conf.setdefault("connections", 8)
    conf.setdefault("max_active", 3)
    save_config(conf)
    return conf


def save_config(conf):
    with _conf_lock:
        tmp = CONF_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(conf, f, indent=2)
        os.replace(tmp, CONF_FILE)
        try:
            os.chmod(CONF_FILE, 0o600)
        except OSError:
            pass


CONF = load_config()


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------
class Stub:
    """Stands in for a finished download restored from disk."""
    state = "done"
    error = None
    speed = 0.0
    eta = None

    def __init__(self, path, total):
        self.path, self.total, self.downloaded = path, total, total
        self.percent = 100.0

    def start(self):
        pass

    def pause(self):
        pass


KNOWN_VIDEO = media.MEDIA_HOSTS


def detect_kind(url):
    if torrent.is_torrent_url(url):
        return "torrent"
    host = urllib.parse.urlparse(url).hostname or ""
    if KNOWN_VIDEO.search(host):
        return "media"
    return "direct"


class Job:
    def __init__(self, url, kind, out_dir, connections, quality="best", jid=None, created=None):
        self.id = jid or uuid.uuid4().hex[:10]
        self.url, self.kind, self.out_dir = url, kind, out_dir
        self.connections, self.quality = connections, quality
        self.created = created or time.time()
        self.hold = False  # user paused while still queued
        self.start_error = None
        if kind == "torrent":
            self.obj = torrent.TorrentDownload(url, out_dir)
        elif kind == "media":
            self.obj = media.MediaDownload(url, out_dir, quality)
        else:
            self.obj = engine.Download(url, out_dir, connections)

    # --- control -----------------------------------------------------------
    def begin(self):
        """Actually start (called by scheduler, in its own thread)."""
        def go():
            try:
                self.obj.start()
            except Exception as e:  # noqa: BLE001
                self.obj.state, self.obj.error = "error", str(e)
        threading.Thread(target=go, daemon=True).start()

    def pause(self):
        if self.obj.state == "running":
            self.obj.pause()
        elif self.obj.state == "queued":
            self.hold = True

    def resume(self):
        self.hold = False
        if self.obj.state in ("paused", "error"):
            self.obj.state = "queued"

    def remove(self, delete_files=False):
        self.obj.pause()
        if self.kind == "torrent" and hasattr(self.obj, "remove"):
            self.obj.remove(delete_files)
        else:
            j = getattr(self.obj, "join", None)
            if j:
                try:
                    j(5)
                except TypeError:
                    j()
        p = getattr(self.obj, "path", None)
        if p:
            for f in (p + ".ghostdl", p + ".part"):
                if os.path.exists(f):
                    os.remove(f)
            if delete_files or self.obj.state != "done":
                if os.path.isfile(p):
                    os.remove(p)
                elif delete_files and os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)

    # --- view ----------------------------------------------------------------
    @property
    def state(self):
        s = self.obj.state
        return "paused" if s == "queued" and self.hold else s

    def to_dict(self):
        o = self.obj
        path = getattr(o, "path", None)
        eta = o.eta
        return {
            "id": self.id, "url": self.url, "kind": self.kind, "state": self.state,
            "name": os.path.basename(path) if path else (getattr(o, "filename", None) or self.url),
            "total": o.total, "downloaded": o.downloaded, "percent": round(o.percent, 2),
            "speed": o.speed, "eta": eta, "error": o.error, "created": self.created,
            "peers": getattr(o, "peers", None),
            "connections": self.connections if self.kind == "direct" else None,
            "quality": self.quality if self.kind == "media" else None,
            "dir": self.out_dir,
            "downloadable": self.state == "done" and bool(path) and os.path.isfile(path),
        }

    def spec(self):
        path = getattr(self.obj, "path", None)
        return {"id": self.id, "url": self.url, "kind": self.kind, "dir": self.out_dir,
                "connections": self.connections, "quality": self.quality,
                "created": self.created, "done": self.obj.state == "done",
                "path": path, "total": self.obj.total}


class Manager:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.RLock()
        self._load()
        threading.Thread(target=self._loop, daemon=True).start()

    def _load(self):
        try:
            with open(JOBS_FILE) as f:
                specs = json.load(f)
        except (OSError, ValueError):
            return
        for s in specs:
            try:
                job = Job(s["url"], s["kind"], s["dir"], s["connections"], s["quality"],
                          s["id"], s["created"])
            except Exception:  # backend missing now; skip
                continue
            if s.get("done") and s.get("path"):
                job.obj = Stub(s["path"], s.get("total", 0))
            else:
                job.hold = True  # restored jobs wait for the user to resume
                job.obj.state = "paused"
            self.jobs[job.id] = job

    def save(self):
        with self.lock:
            specs = [j.spec() for j in self.jobs.values()]
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(specs, f)
        os.replace(tmp, JOBS_FILE)

    def add(self, url, kind="auto", out_dir=None, connections=None, quality="best", start=True):
        url = url.strip()
        if not re.match(r"^(https?|ftp)://|^magnet:", url, re.I):
            raise ValueError(f"Not a supported link: {url[:80]}")
        if kind == "auto":
            kind = detect_kind(url)
        if kind == "torrent" and not torrent.AVAILABLE:
            raise RuntimeError("Torrent support needs libtorrent. " + torrent.INSTALL_HINT)
        if kind == "media" and not media.AVAILABLE:
            raise RuntimeError("Video-site support needs yt-dlp. " + media.INSTALL_HINT)
        out_dir = os.path.abspath(os.path.expanduser(out_dir or CONF["download_dir"]))
        connections = int(connections or CONF["connections"])
        connections = max(1, min(32, connections))
        job = Job(url, kind, out_dir, connections, quality)
        if not start:
            job.hold = True
        with self.lock:
            self.jobs[job.id] = job
        self.save()
        return job

    def get(self, jid):
        with self.lock:
            return self.jobs.get(jid)

    def remove(self, jid, delete_files=False):
        with self.lock:
            job = self.jobs.pop(jid, None)
        if job:
            job.remove(delete_files)
            self.save()
        return bool(job)

    def listing(self):
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)
        return [j.to_dict() for j in jobs]

    def _loop(self):
        last, snap = 0, None
        while True:
            time.sleep(0.5)
            with self.lock:
                jobs = list(self.jobs.values())
            for j in jobs:
                tick = getattr(j.obj, "_tick", None)
                if tick:
                    try:
                        tick()
                    except Exception:  # noqa: BLE001
                        pass
            active = sum(1 for j in jobs if j.obj.state == "running")
            for j in sorted(jobs, key=lambda j: j.created):
                if active >= CONF["max_active"]:
                    break
                if j.obj.state == "queued" and not j.hold:
                    j.obj.state = "starting"  # claimed; real state set by start()
                    j.begin()
                    active += 1
            now_snap = tuple((j.id, j.obj.state, j.hold) for j in jobs)
            if now_snap != snap or time.time() - last > 3:
                try:
                    self.save()
                except OSError:
                    pass
                last, snap = time.time(), now_snap


MGR = None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "DivineIDM/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # --- helpers -----------------------------------------------------------
    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if self.server.lan:
            return True
        return host in ("localhost", "127.0.0.1", "[::1]", "::1")

    def _authed(self, query):
        tok = self.headers.get("X-Token") or query.get("token", [""])[0]
        return secrets.compare_digest(tok, CONF["token"])

    def _is_loopback(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def _cors(self):
        # only the browser extension (chrome-extension:// / moz-extension://) may call cross-origin
        origin = self.headers.get("Origin", "")
        if origin.startswith(("chrome-extension://", "moz-extension://")):
            return {"Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Headers": "Content-Type, X-Token",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Vary": "Origin"}
        return {}

    # --- routing -----------------------------------------------------------
    def do_OPTIONS(self):
        self._send(204, b"", extra=self._cors())

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._err(403, "bad host")
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path

        if path == "/":
            if not (self._is_loopback() or self._authed(q)):
                return self._send(401, "<h3>DIVINE IDM</h3><p>Open the full URL printed in the server "
                                  "console (it includes ?token=...).</p>", "text/html")
            html = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
            html = html.replace("__TOKEN__", CONF["token"])
            return self._send(200, html, "text/html; charset=utf-8")

        if path in ("/manifest.webmanifest", "/sw.js", "/icon.svg"):
            fn = path.lstrip("/")
            ctype = mimetypes.guess_type(fn)[0] or "application/octet-stream"
            if fn.endswith(".webmanifest"):
                ctype = "application/manifest+json"
            with open(os.path.join(STATIC, fn), "rb") as f:
                return self._send(200, f.read(), ctype)

        if not self._authed(q):
            return self._err(401, "unauthorized")

        if path == "/api/state":
            return self._send(200, {
                "jobs": MGR.listing(),
                "config": {k: CONF[k] for k in ("download_dir", "connections", "max_active")},
                "capabilities": {
                    "direct": True, "ftp": True,
                    "torrent": torrent.AVAILABLE, "torrent_hint": torrent.INSTALL_HINT,
                    "media": media.AVAILABLE, "media_hint": media.INSTALL_HINT,
                },
            }, extra=self._cors())

        m = re.fullmatch(r"/api/file/([0-9a-f]{10})", path)
        if m:
            return self._serve_file(m.group(1))
        self._err(404, "not found")

    def _serve_file(self, jid):
        job = MGR.get(jid)
        p = job and getattr(job.obj, "path", None)
        if not p or not os.path.isfile(p):
            return self._err(404, "file not available")
        size = os.path.getsize(p)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end:
                    return self._send(416, b"", extra={"Content-Range": f"bytes */{size}"})
                code = 206
        name = os.path.basename(p)
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(p)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(p, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    buf = f.read(min(1 << 16, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self._host_ok():
            return self._err(403, "bad host")
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self._authed(q):
            return self._err(401, "unauthorized")
        try:
            body = self._json_body()
        except (ValueError, json.JSONDecodeError):
            return self._err(400, "invalid JSON")
        path = u.path
        cors = self._cors()
        try:
            if path == "/api/add":
                urls = body.get("urls") or ([body["url"]] if body.get("url") else [])
                added, errors = [], []
                for url in urls:
                    try:
                        job = MGR.add(url, body.get("kind", "auto"), body.get("dir"),
                                      body.get("connections"), body.get("quality", "best"))
                        added.append(job.to_dict())
                    except Exception as e:  # noqa: BLE001
                        errors.append({"url": url, "error": str(e)})
                return self._send(200 if added else 400, {"added": added, "errors": errors},
                                  extra=cors)

            if path == "/api/probe":
                url = (body.get("url") or "").strip()
                kind = body.get("kind", "auto")
                if kind == "auto":
                    kind = detect_kind(url)
                if kind == "media":
                    info = media.probe(url)
                elif kind == "torrent":
                    info = {"title": "Torrent / magnet link"}
                else:
                    d = engine.Download(url)
                    d._probe()
                    info = {"title": d.filename, "size": d.total,
                            "resumable": d.accept_ranges}
                info["kind"] = kind
                return self._send(200, info)

            if path == "/api/settings":
                if "download_dir" in body:
                    CONF["download_dir"] = os.path.abspath(os.path.expanduser(body["download_dir"]))
                if "connections" in body:
                    CONF["connections"] = max(1, min(32, int(body["connections"])))
                if "max_active" in body:
                    CONF["max_active"] = max(1, min(16, int(body["max_active"])))
                save_config(CONF)
                return self._send(200, {"ok": True})

            m = re.fullmatch(r"/api/jobs/([0-9a-f]{10})/(pause|resume|remove)", path)
            if m:
                job = MGR.get(m.group(1))
                if not job:
                    return self._err(404, "no such download")
                act = m.group(2)
                if act == "pause":
                    job.pause()
                elif act == "resume":
                    job.resume()
                else:
                    MGR.remove(job.id, bool(body.get("delete_files")))
                return self._send(200, {"ok": True})

            if path in ("/api/pause_all", "/api/resume_all", "/api/clear_done"):
                for j in list(MGR.jobs.values()):
                    if path == "/api/pause_all":
                        j.pause()
                    elif path == "/api/resume_all":
                        j.resume()
                    elif j.state == "done":
                        MGR.remove(j.id)
                return self._send(200, {"ok": True})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)}, extra=cors)
        self._err(404, "not found")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    lan = False


def main():
    global MGR
    ap = argparse.ArgumentParser(description="DIVINE IDM server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="open the browser")
    a = ap.parse_args()
    MGR = Manager()
    srv = Server((a.host, a.port), Handler)
    srv.lan = a.host not in ("127.0.0.1", "localhost", "::1")
    shown = "localhost" if not srv.lan else a.host
    url = f"http://{shown}:{a.port}/" + (f"?token={CONF['token']}" if srv.lan else "")
    print("DIVINE IDM is running")
    print(f"  open:      {url}")
    print(f"  token:     {CONF['token']}   (paste into the browser extension)")
    print(f"  saving to: {CONF['download_dir']}")
    print(f"  torrents:  {'yes' if torrent.AVAILABLE else 'no  -> ' + torrent.INSTALL_HINT}")
    print(f"  video:     {'yes' if media.AVAILABLE else 'no  -> ' + media.INSTALL_HINT}")
    if srv.lan:
        print("  WARNING: listening on the network. Anyone with the token can control it.")
    if a.open:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\npausing downloads and saving state...")
        for j in list(MGR.jobs.values()):
            j.pause()
        time.sleep(1)
        MGR.save()


if __name__ == "__main__":
    main()
