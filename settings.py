"""Persistent user settings (settings.json next to the app)."""
import json
import os
import threading

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
DEFAULTS = {"net_mode": "auto", "net_proxy": "", "restrict_media": False,
            "max_concurrent": 3, "phone_friendly": True, "threads": 8}
_lock = threading.Lock()
_data = None


def _load():
    global _data
    if _data is None:
        _data = dict(DEFAULTS)
        try:
            with open(FILE, encoding="utf-8") as f:
                _data.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
    return _data


def get(key=None):
    with _lock:
        d = _load()
        return dict(d) if key is None else d[key]


def update(**kw):
    """Validate + save. Unknown keys are ignored."""
    with _lock:
        d = _load()
        for k, v in kw.items():
            if k not in DEFAULTS:
                continue
            if isinstance(DEFAULTS[k], bool):
                v = bool(v)
            elif isinstance(DEFAULTS[k], int):
                v = int(v)
                v = max(1, min(v, 6 if k == "max_concurrent" else 16))
            elif k == "net_mode" and v not in ("auto", "direct", "custom"):
                continue
            d[k] = v
        try:
            with open(FILE, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
        except OSError:
            pass
        return dict(d)
