"""DIVINE IDM - Flask server."""
import importlib.util
import sys

_REQUIRED = {"flask": "flask", "requests": "requests", "yt_dlp": "yt-dlp[default]"}
_missing = [pkg for mod, pkg in _REQUIRED.items() if importlib.util.find_spec(mod) is None]
if _missing:
    print("\nMissing packages: " + ", ".join(_missing))
    print("Install everything with:   python -m pip install -r requirements.txt\n")
    raise SystemExit(1)

import argparse  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import webbrowser  # noqa: E402
from urllib.parse import urlparse

import requests
from flask import Flask, abort, jsonify, render_template, request, send_file

import network
import settings
from manager import DownloadManager, engine_status
from share import share_file
from utils import default_downloads_dir, ffmpeg_path, strip_ansi

APP_NAME = "DIVINE IDM"
PORT = 5001
DOWNLOAD_DIR = default_downloads_dir()

network.load()                                    # apply saved proxy / direct-connection choice
app = Flask(__name__)
manager = DownloadManager(DOWNLOAD_DIR)


def _valid_url(url):
    p = urlparse(url)
    return p.scheme in ("http", "https") and bool(p.netloc)


def _fail(msg, code=400, **extra):
    return jsonify(success=False, error=strip_ansi(msg), **extra), code


def _body():
    return request.get_json(silent=True) or {}


def _task_file(task_id):
    t = manager.tasks.get(task_id)
    if not t or not os.path.exists(t.save_path) or not manager.inside(t.save_path):
        return None, None
    return t, t.save_path


@app.after_request
def allow_local_pages(resp):
    """Let the UI call this API even if the page is served from another local port
    (e.g. a dev server on :50001). Only loopback origins are allowed."""
    origin = request.headers.get("Origin", "")
    if urlparse(origin).hostname in ("localhost", "127.0.0.1", "::1"):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Vary"] = "Origin"
    return resp


@app.route("/api/health")
def health():
    """Used by the UI's connection indicator and by the launcher."""
    return jsonify(app="divine-idm", name=APP_NAME, ok=True, port=request.host.rsplit(":", 1)[-1],
                   download_dir=DOWNLOAD_DIR)


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/")
def index():
    return render_template("index.html", app_name=APP_NAME, download_dir=DOWNLOAD_DIR)


# ---------- add download (2 steps) ----------
@app.route("/api/inspect", methods=["POST"])
def inspect_link():
    """Step 1: classify the link + return qualities/metadata (no download)."""
    url = (_body().get("url") or "").strip()
    if not _valid_url(url):
        return _fail("Enter a valid http(s) URL")
    try:
        return jsonify(success=True, url=url, **manager.inspect(url))
    except network.NetworkProblem as e:
        return _fail(str(e), network=True, details=e.details)
    except ValueError as e:
        return _fail(str(e)[:500])
    except requests.RequestException as e:
        return _fail(f"Could not reach that link: {e}")
    except Exception as e:                          # never leave the UI stuck on "Analyzing"
        return _fail(f"Unexpected error: {e}"[:300], 500)


@app.route("/api/download", methods=["POST"])
def add_download():
    """Step 2: start the download using the user's chosen option."""
    d = _body()
    url = (d.get("url") or "").strip()
    if not _valid_url(url):
        return _fail("Enter a valid http(s) URL")
    try:
        task = manager.add(url, d.get("option_id"), (d.get("name") or "").strip(),
                           d.get("threads"), d.get("compat"))
    except network.NetworkProblem as e:
        return _fail(str(e), network=True, details=e.details)
    except (ValueError, requests.RequestException) as e:
        return _fail(str(e)[:300])
    return jsonify(success=True, id=task.id, filename=task.filename)


# ---------- task controls ----------
@app.route("/api/<action>/<task_id>", methods=["POST"])
def control(action, task_id):
    if action == "rename":
        try:
            return jsonify(success=True, filename=manager.rename(task_id, _body().get("name", "")))
        except (ValueError, OSError) as e:
            return _fail(str(e))
    if action == "redownload":
        try:
            return jsonify(success=True, id=manager.redownload(task_id).id)
        except ValueError as e:
            return _fail(str(e))
    if action == "delete":
        manager.delete(task_id, bool(_body().get("file")))
        return jsonify(success=True)
    fn = {"pause": manager.pause, "resume": manager.resume, "cancel": manager.cancel}.get(action)
    if not fn:
        return _fail("Unknown action", 404)
    fn(task_id)
    return jsonify(success=True)


@app.route("/api/pause-all", methods=["POST"])
def pause_all():
    manager.pause_all()
    return jsonify(success=True)


@app.route("/api/resume-all", methods=["POST"])
def resume_all():
    manager.resume_all()
    return jsonify(success=True)


@app.route("/api/clear-finished", methods=["POST"])
def clear_finished():
    manager.clear_finished()
    return jsonify(success=True)


# ---------- files: open / preview / send to phone ----------
@app.route("/api/open/<task_id>", methods=["POST"])
def open_item(task_id):
    """Open the file, or show it in the file manager (this app runs on your own PC)."""
    t, path = _task_file(task_id)
    if not t:
        return _fail("The file no longer exists on disk", 404)
    select = bool(_body().get("folder"))
    try:
        if sys.platform.startswith("win"):
            if select:
                subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
            else:
                os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if select else ["open", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) if select else path])
    except Exception as e:
        return _fail(f"Could not open it here: {e}")
    return jsonify(success=True)


@app.route("/api/preview/<task_id>")
def preview(task_id):
    t, path = _task_file(task_id)
    if not t or t.status != "Completed" or os.path.isdir(path):
        abort(404)
    return send_file(path, conditional=True)        # supports Range, so video seeking works


@app.route("/api/share/<task_id>", methods=["POST"])
def share(task_id):
    t, path = _task_file(task_id)
    if not t or os.path.isdir(path) or t.status != "Completed":
        return _fail("Only finished files can be sent to a phone")
    try:
        return jsonify(success=True, filename=t.filename, **share_file(path))
    except OSError as e:
        return _fail(str(e))


# ---------- info / settings / network ----------
@app.route("/api/downloads")
def downloads():
    return jsonify(sorted(manager.list(), key=lambda t: t["id"], reverse=True))


@app.route("/api/engines")
def engines():
    return jsonify(engine_status())


@app.route("/api/disk")
def disk():
    return jsonify(manager.disk())


@app.route("/api/settings", methods=["GET", "POST"])
def app_settings():
    if request.method == "POST":
        data = settings.update(**{k: v for k, v in _body().items() if not k.startswith("net_")})
        manager.set_limit(data["max_concurrent"])
    return jsonify(settings.get())


@app.route("/api/network", methods=["GET", "POST"])
def network_settings():
    if request.method == "POST":
        d = _body()
        try:
            network.set_mode(d.get("mode", "auto"), d.get("proxy", ""))
        except ValueError as e:
            return _fail(str(e))
    return jsonify(network.status())


def _port_free(port, host="127.0.0.1"):
    """Stricter than Flask's own check: also catches Windows-reserved/blocked ports."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _is_our_app(port):
    try:
        r = requests.get(f"http://127.0.0.1:{port}/api/health", timeout=1.5, proxies={"http": None, "https": None})
        return r.ok and r.json().get("app") == "divine-idm"
    except Exception:
        return False


def _check():
    """`python app.py --check`: print what's installed and whether the network works."""
    from manager import engine_status
    print(f"{APP_NAME} environment check")
    print(f"  Python      : {sys.version.split()[0]}  ({sys.executable})")
    print(f"  Downloads   : {DOWNLOAD_DIR}  (exists: {os.path.isdir(DOWNLOAD_DIR)})")
    print(f"  ffmpeg      : {ffmpeg_path() or 'NOT FOUND (pip install imageio-ffmpeg)'}")
    print("  Engines     : " + ", ".join(f"{k}={'ok' if v else 'missing'}" for k, v in engine_status().items()))
    free = [p for p in range(PORT, PORT + 5) if _port_free(p)]
    print(f"  Ports       : free in {PORT}-{PORT + 4}: {free or 'NONE (try --port 8080)'}")
    st = network.status()
    print(f"  Internet    : {'OK' if st['ok'] else 'PROBLEM'} - {st['message']}")


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", PORT)), help=f"preferred port (default {PORT})")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1 = this PC only)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    ap.add_argument("--check", action="store_true", help="print an environment/network report and exit")
    args = ap.parse_args()
    if args.check:
        return _check()

    if _is_our_app(args.port):                     # already running: don't start a second copy
        url = f"http://127.0.0.1:{args.port}"
        print(f"{APP_NAME} is already running at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    port = args.port
    if not _port_free(port):                       # busy or blocked by Windows: use the next free one
        alt = next((p for p in range(port + 1, port + 20) if _port_free(p)), None)
        if alt is None:
            raise SystemExit(f"Port {port} and the next 19 ports are unavailable. Try: python app.py --port 8080")
        print(f"Port {port} is busy or blocked, using {alt} instead.")
        port = alt
    url = f"http://127.0.0.1:{port}"
    print("=" * 58)
    print(f"  {APP_NAME} is running.  Open:  {url}")
    print(f"  Saving to: {DOWNLOAD_DIR}")
    print("  Keep this window open. Press Ctrl+C to stop.")
    print("=" * 58)
    if not args.no_browser:
        threading.Timer(1.2, webbrowser.open, args=[url]).start()
    app.run(host=args.host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
