"""Network diagnostics + proxy handling.

Fixes "[WinError 10061] No connection could be made because the target machine
actively refused it". The usual causes on Windows are:
  * a leftover/dead system proxy (VPN or proxy tools often forget to remove it)
  * DNS or the hosts file pointing the site to 127.0.0.1 / 0.0.0.0
  * a firewall / antivirus / ISP blocking the connection

Modes (saved in settings.json):  auto (default) | direct | custom
The choice is pushed into environment variables, so requests, urllib, yt-dlp,
pytubefix, you-get and gallery-dl (all started by this app) follow it.
"""
import json
import os
import socket
import time
import urllib.request
from urllib.parse import urlparse

import requests

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
              "NO_PROXY", "no_proxy")
_ORIGINAL = {k: os.environ.get(k) for k in PROXY_VARS}     # what the user's system had
_state = {"mode": "auto", "proxy": ""}
_ok_until = {}                                              # host -> time until which it's known reachable
UA = {"User-Agent": "Mozilla/5.0 (DIVINE-GOPEH)"}


class NetworkProblem(Exception):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


# ---------- settings ----------
def load():
    import settings
    _state["mode"] = settings.get("net_mode")
    _state["proxy"] = settings.get("net_proxy")
    apply()


def _save():
    import settings
    settings.update(net_mode=_state["mode"], net_proxy=_state["proxy"])


def set_mode(mode, proxy=""):
    if mode not in ("auto", "direct", "custom"):
        raise ValueError("Unknown network mode")
    proxy = (proxy or "").strip()
    if mode == "custom":
        if not proxy:
            raise ValueError("Enter a proxy address, e.g. http://127.0.0.1:10809")
        if "://" not in proxy:
            proxy = "http://" + proxy
    _state.update(mode=mode, proxy=proxy if mode == "custom" else "")
    _ok_until.clear()
    _save()
    apply()


def get_state():
    return dict(_state)


def apply():
    """Push the chosen mode into the environment (inherited by every engine)."""
    for k in PROXY_VARS:
        os.environ.pop(k, None)
    if _state["mode"] == "custom":
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ[k] = _state["proxy"]
    elif _state["mode"] == "direct":
        # A non-empty NO_PROXY also stops Windows from reading the registry proxy
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
    else:
        for k, v in _ORIGINAL.items():
            if v is not None:
                os.environ[k] = v


def ytdlp_proxy():
    """Value for yt-dlp's `proxy` option (None = leave it to the environment)."""
    return {"direct": "", "custom": _state["proxy"]}.get(_state["mode"])


def system_proxies():
    """Proxies the OS/environment would use if we did nothing (Windows registry included)."""
    saved = {k: os.environ.get(k) for k in PROXY_VARS}
    try:
        for k in PROXY_VARS:
            os.environ.pop(k, None)
        for k, v in _ORIGINAL.items():
            if v is not None:
                os.environ[k] = v
        return {k: v for k, v in urllib.request.getproxies().items() if k != "no" and v}
    finally:
        for k in PROXY_VARS:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ---------- probes ----------
def _probe(url, direct=False):
    p = urlparse(url)
    root = f"{p.scheme or 'https'}://{p.netloc}/"
    try:
        s = requests.Session()
        s.trust_env = not direct
        s.head(root, timeout=8, allow_redirects=False, headers=UA)
        return True
    except requests.RequestException:
        return False


def _can_connect(host, port, timeout=6):
    try:
        socket.create_connection((host, port), timeout).close()
        return True
    except OSError:
        return False


def diagnose(host, port=443):
    d = {"host": host, "mode": _state["mode"], "dns": None, "dns_blocked": False,
         "proxies": system_proxies(), "proxy_alive": None, "direct_ok": False}
    try:
        d["dns"] = socket.gethostbyname(host)
        d["dns_blocked"] = d["dns"].startswith("127.") or d["dns"] == "0.0.0.0"
    except OSError:
        pass
    if d["proxies"]:
        pu = urlparse(next(iter(d["proxies"].values())))
        if pu.hostname:
            d["proxy_alive"] = _can_connect(pu.hostname, pu.port or 80, 4)
    d["direct_ok"] = _can_connect(host, port)
    return d


def explain(host, d):
    if d["dns_blocked"]:
        return (f"Your DNS or Windows 'hosts' file sends {host} to {d['dns']}, a local/blocked address, "
                f"so the connection is refused. Check C:\\Windows\\System32\\drivers\\etc\\hosts, your DNS "
                f"(try 8.8.8.8 or 1.1.1.1) or any ad-blocker/parental-control tool.")
    if d["proxies"] and d["proxy_alive"] is False:
        return (f"Windows is set to use a proxy ({next(iter(d['proxies'].values()))}) that is not running "
                f"(often left behind by a VPN/proxy app). Open Network settings below and choose 'Direct connection'.")
    if d["dns"] is None:
        return f"Could not look up {host}. Check that this PC is online, or try another DNS (8.8.8.8 / 1.1.1.1)."
    if not d["direct_ok"]:
        return (f"{host} did not accept the connection from this PC (refused/timed out). A firewall, antivirus "
                f"web-shield, VPN or your ISP is probably blocking it. Allow python.exe in the firewall, pause "
                f"the antivirus web protection, or connect through a VPN/proxy (Network settings below).")
    return f"Could not connect to {host} with the current network settings. Try the options in Network settings."


def preflight(url):
    """Fail fast with a clear explanation if the site can't be reached.
    Returns a note string if a dead system proxy was bypassed automatically."""
    pu = urlparse(url)
    host = pu.hostname
    key = f"{host}:{pu.port or ''}"
    if not host or _ok_until.get(key, 0) > time.time():
        return None
    if _probe(url):
        _ok_until[key] = time.time() + 60
        return None
    if _state["mode"] == "auto" and system_proxies() and _probe(url, direct=True):
        _state["mode"] = "direct"          # the system proxy is dead but direct works
        _save()
        apply()
        _ok_until[key] = time.time() + 60
        return "Your system proxy wasn't responding, so DIVINE IDM switched to a direct connection."
    d = diagnose(host, pu.port or (80 if pu.scheme == 'http' else 443))
    raise NetworkProblem(explain(host, d), d)


def status():
    """For the Network settings panel."""
    try:
        note = preflight("https://www.youtube.com/")
        return {**_state, "ok": True, "message": note or "Connection to the internet works.",
                "system_proxies": system_proxies()}
    except NetworkProblem as e:
        return {**_state, "ok": False, "message": str(e), "details": e.details,
                "system_proxies": system_proxies()}
