"""'Send to phone': a tiny LAN-only file server.

The phone opens a link in its browser and downloads the file itself. Android
then registers it with the system (Files, Gallery, WhatsApp, Music...), which
often doesn't happen when files are just copied from a PC.

Only files you explicitly share are served, each under a random token that
expires after an hour. The main app (port 5001) stays on localhost.
"""
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

TTL = 3600
_tokens = {}            # token -> (path, expires)
_server = {"port": None}
_lock = threading.Lock()


def lan_ips():
    ips = []
    try:                                    # UDP "connect" sends nothing; just picks the LAN interface
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    seen = []
    for ip in ips:
        if not ip.startswith("127.") and ip not in seen:
            seen.append(ip)
    return sorted(seen, key=lambda i: not i.startswith(("192.168.", "10.", "172.")))


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _file(self):
        m = re.match(r"^/d/([\w-]+)/", self.path)
        entry = _tokens.get(m.group(1)) if m else None
        if not entry or entry[1] < time.time() or not os.path.isfile(entry[0]):
            self.send_error(404, "Link expired or not found")
            return None
        return entry[0]

    def _send(self, body):
        path = self._file()
        if not path:
            return
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                start = max(0, size - int(rng.group(2)))
            end = min(end, size - 1)
            code = 206
        name = os.path.basename(path)
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if body:
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0 and (chunk := f.read(min(65536, left))):
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except OSError:
                pass

    def do_GET(self):
        self._send(True)

    def do_HEAD(self):
        self._send(False)


def _ensure_server(base_port):
    with _lock:
        if _server["port"]:
            return _server["port"]
        for port in range(base_port, base_port + 20):
            try:
                srv = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
            except OSError:
                continue
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            _server["port"] = port
            return port
    raise OSError("No free port for the phone-transfer server")


def share_file(path, base_port=5100):
    """-> {"urls": [...], "port": n}. Starts the LAN server on first use."""
    port = _ensure_server(base_port)
    token = secrets.token_urlsafe(9)
    _tokens[token] = (path, time.time() + TTL)
    for t in [t for t, (_, exp) in _tokens.items() if exp < time.time()]:
        _tokens.pop(t, None)
    name = quote(os.path.basename(path))
    return {"port": port, "urls": [f"http://{ip}:{port}/d/{token}/{name}" for ip in lan_ips()] or
            [f"http://127.0.0.1:{port}/d/{token}/{name}"], "minutes": TTL // 60}
