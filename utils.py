"""Shared helpers: formatting, filename extraction, link probing."""
import mimetypes
import os
import re
import shutil
import sys
import time
import unicodedata
from urllib.parse import parse_qs, unquote, urlparse

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (DIVINE-GOPEH)"}
UNKNOWN_MIME = ("", "application/octet-stream", "binary/octet-stream")
MIME_OVERRIDES = {"image/png": ".png", "image/jpeg": ".jpg",
                  "audio/mpeg": ".mp3", "video/mp4": ".mp4"}
# Names that are routes/pages rather than real filenames ("watch", "download"...)
GENERIC = {"watch", "download", "get", "file", "video", "audio", "image",
           "stream", "play", "view", "index", "media", "content"}


def fmt_bytes(n):
    if not n:
        return "Unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def fmt_eta(seconds):
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def fmt_duration(sec):
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def clean_filename(name):
    """Make a name safe for Windows, Android and SD cards: no illegal characters,
    no emoji/non-BMP symbols, no control chars, no reserved names."""
    name = "".join(ch for ch in (name or "") if ord(ch) <= 0xFFFF and unicodedata.category(ch) not in ("Cc", "Cs", "Co"))
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")[:150].rstrip(". ")
    if name.split(".")[0].lower() in RESERVED:
        name = "_" + name
    return name


def unique_path(directory, filename):
    base, ext = os.path.splitext(filename)
    path, i = os.path.join(directory, filename), 1
    while os.path.exists(path):
        path = os.path.join(directory, f"{base} ({i}){ext}")
        i += 1
    return path


def filename_from_disposition(cd):
    """Extract the filename from a Content-Disposition header."""
    if not cd:
        return None
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", cd, re.I)
    if m:
        raw = m.group(2).strip().strip('"')
        try:
            return unquote(raw, encoding=m.group(1) or "utf-8")
        except LookupError:
            return unquote(raw)
    m = (re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I)
         or re.search(r"filename\s*=\s*([^;]+)", cd, re.I))
    return unquote(m.group(1).strip()) if m else None


def guess_ext(ctype):
    if ctype in MIME_OVERRIDES:
        return MIME_OVERRIDES[ctype]
    if ctype in UNKNOWN_MIME or ctype.startswith("text/html"):
        return ""
    ext = mimetypes.guess_extension(ctype) or ""
    return {".jpe": ".jpg"}.get(ext, ext)


def probe_url(url):
    """HEAD request (GET Range fallback) -> final URL, type, filename, size,
    and whether the server supports `Accept-Ranges: bytes`."""
    r, ok = None, False
    try:
        r = requests.head(url, allow_redirects=True, timeout=15, headers=HEADERS)
        ok = r.status_code < 400
    except requests.RequestException:
        pass
    ctype = lambda x: x.headers.get("content-type", "").split(";")[0].strip().lower()
    if not ok or ctype(r) in UNKNOWN_MIME or "content-length" not in r.headers:
        # Some servers reject HEAD or hide headers: ask for 1 byte via GET
        r = requests.get(url, headers={**HEADERS, "Range": "bytes=0-0"},
                         stream=True, timeout=15, allow_redirects=True)
        r.close()
        r.raise_for_status()
        if r.status_code == 206 and "/" in r.headers.get("content-range", ""):
            tot = r.headers["content-range"].split("/")[-1]
            total, ranged = (int(tot) if tot.isdigit() else 0), True
        else:
            total, ranged = int(r.headers.get("content-length", 0) or 0), False
    else:
        total = int(r.headers["content-length"])
        ranged = r.headers.get("accept-ranges", "").lower() == "bytes"
    return {"url": url, "final_url": r.url, "content_type": ctype(r),
            "disposition_name": filename_from_disposition(r.headers.get("content-disposition")),
            "total_size": total, "ranged": ranged and total > 0}


def pick_filename(info, user_name=None):
    """Priority: user's name > Content-Disposition > URL path > URL query > timestamp.
    Missing extensions are added from the Content-Type."""
    guessed = guess_ext(info["content_type"])

    def candidates():
        yield info.get("disposition_name")
        for u in (info["final_url"], info["url"]):
            p = urlparse(u)
            yield unquote(p.path.rstrip("/").split("/")[-1])
            q = parse_qs(p.query)
            for k in ("filename", "file", "name", "title"):
                if k in q:
                    yield q[k][0]

    original = None
    for c in candidates():
        name = clean_filename(c)
        if name and os.path.splitext(name)[0].lower() not in GENERIC:
            original = name
            break

    def ext_of(n):
        e = os.path.splitext(n or "")[1]
        return e if 1 < len(e) <= 6 and " " not in e else ""

    if user_name and clean_filename(user_name):
        name = clean_filename(user_name)
        return name if ext_of(name) else name + (ext_of(original) or guessed)
    if original:
        return original if ext_of(original) else original + guessed
    return f"download_{time.strftime('%Y%m%d_%H%M%S')}{guessed}"


# ---------------- environment helpers ----------------
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text):
    """Remove terminal colour codes (they show up as garbage boxes in the UI)."""
    return _ANSI.sub("", str(text or ""))


_FF = {}


def ffmpeg_path():
    """System ffmpeg if present, otherwise the one bundled by `imageio-ffmpeg`."""
    if "p" not in _FF:
        p = shutil.which("ffmpeg")
        if not p:
            try:
                import imageio_ffmpeg
                p = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                p = None
        _FF["p"] = p
    return _FF["p"]


def default_downloads_dir():
    """The real Downloads folder (handles Windows folders moved to OneDrive/another drive)."""
    if os.environ.get("DIVINE_DOWNLOAD_DIR"):
        return os.path.expanduser(os.environ["DIVINE_DOWNLOAD_DIR"])
    home = os.path.expanduser("~")
    if os.name == "nt":
        try:
            import ctypes
            import uuid

            class GUID(ctypes.Structure):
                _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]
            guid = GUID.from_buffer_copy(uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le)
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(ptr)) == 0:
                path = ptr.value
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                if path:
                    return path
        except Exception:
            pass
    elif sys.platform.startswith("linux"):
        try:
            with open(os.path.join(home, ".config", "user-dirs.dirs"), encoding="utf-8") as f:
                m = re.search(r'XDG_DOWNLOAD_DIR="?([^"\n]+)"?', f.read())
            if m:
                return m.group(1).replace("$HOME", home)
        except OSError:
            pass
    return os.path.join(home, "Downloads")


# ---------------- file categories (sidebar, like IDM) ----------------
CATEGORY_EXTS = {
    "Musics": {"mp3", "m4a", "aac", "flac", "wav", "ogg", "opus", "wma"},
    "Videos": {"mp4", "mkv", "webm", "avi", "mov", "flv", "ts", "m4v", "wmv", "3gp"},
    "Images": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "avif"},
    "Compressed": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso"},
    "Programs": {"exe", "msi", "dmg", "pkg", "deb", "rpm", "appimage"},
    "APKs": {"apk", "xapk", "aab"},
    "Documents": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "epub", "odt", "rtf"},
}
PREVIEW_EXTS = {"mp4", "webm", "m4v", "mov", "mp3", "m4a", "ogg", "opus", "wav", "png", "jpg", "jpeg", "gif", "webp"}


def category_of(filename, kind="direct"):
    if kind == "gallery":
        return "Images"
    ext = os.path.splitext(filename or "")[1].lstrip(".").lower()
    return next((c for c, exts in CATEGORY_EXTS.items() if ext in exts), "Other")


def is_media_allowed(info):
    """Strict mode: only PNG/JPG images, MP3 audio and MP4 video (exact MIME -> extension map)."""
    if info["content_type"] in MIME_OVERRIDES:
        return True
    if info["content_type"] in UNKNOWN_MIME:
        for n in (info.get("disposition_name"), urlparse(info["final_url"]).path, urlparse(info["url"]).path):
            if os.path.splitext(unquote(n or ""))[1].lower() in (".png", ".jpg", ".jpeg", ".mp3", ".mp4"):
                return True
    return False
