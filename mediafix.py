"""Make downloaded videos play everywhere (Android Gallery, WhatsApp, iPhone).

YouTube's 1440p/4K streams are VP9/AV1 + Opus, which many phone galleries and
WhatsApp reject. Phones want MP4 with H.264 video + AAC audio. This converts
ONLY when needed (already-compatible files are left untouched).
"""
import os
import re
import subprocess

from utils import ffmpeg_path

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".flv", ".mov", ".avi", ".ts", ".m4v")


def probe(ff, path):
    """-> (video_codec, audio_codec, duration_seconds) using `ffmpeg -i`."""
    p = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True, errors="replace")
    v = re.search(r"Video: (\w+)", p.stderr)
    a = re.search(r"Audio: (\w+)", p.stderr)
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
    dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 0
    return (v.group(1) if v else None), (a.group(1) if a else None), dur


def make_phone_friendly(path, ev=None, on_progress=None):
    """-> (final_path, problem_message_or_None)"""
    if not path.lower().endswith(VIDEO_EXTS):
        return path, None
    ff = ffmpeg_path()
    if not ff:
        return path, "ffmpeg not found - could not make this video phone-friendly"
    v, a, dur = probe(ff, path)
    v_ok, a_ok = v == "h264", a in (None, "aac", "mp3")
    if v_ok and a_ok and path.lower().endswith(".mp4"):
        return path, None
    out = os.path.splitext(path)[0] + ".mp4"
    tmp = os.path.splitext(path)[0] + ".fix.mp4"
    cmd = [ff, "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats", "-i", path,
           "-map", "0:v:0", "-map", "0:a:0?"]
    cmd += ["-c:v", "copy"] if v_ok else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "copy"] if a_ok else ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", tmp]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    for line in proc.stdout:
        if ev is not None and ev.is_set():
            proc.kill()
            break
        if line.startswith("out_time_us=") and dur and on_progress:
            try:
                on_progress(min(99.0, int(line.split("=")[1]) / 1e6 / dur * 100))
            except ValueError:
                pass
    proc.wait()
    err = proc.stderr.read().strip()
    if (ev is not None and ev.is_set()) or proc.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        return path, (None if (ev is not None and ev.is_set()) else f"conversion failed: {err[-150:]}")
    os.replace(tmp, out)
    if os.path.abspath(out) != os.path.abspath(path) and os.path.exists(path):
        os.remove(path)
    return out, None
