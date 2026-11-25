"""BitTorrent / magnet support via libtorrent (optional dependency)."""
import os
import threading
import urllib.request

try:
    import libtorrent as lt
except ImportError:  # pragma: no cover
    lt = None

AVAILABLE = lt is not None
INSTALL_HINT = "pip install libtorrent   (or: apt install python3-libtorrent)"

_session = None
_session_lock = threading.Lock()


def _ses():
    global _session
    with _session_lock:
        if _session is None:
            _session = lt.session({"listen_interfaces": "0.0.0.0:6881,[::]:6881",
                                   "enable_dht": True, "enable_lsd": True})
            for r in ("router.bittorrent.com:6881", "router.utorrent.com:6881",
                      "dht.transmissionbt.com:6881"):
                host, port = r.split(":")
                _session.add_dht_router(host, int(port))
        return _session


def is_torrent_url(url):
    u = url.strip().lower()
    return u.startswith("magnet:") or u.split("?")[0].endswith(".torrent")


class TorrentDownload:
    def __init__(self, url, out_dir, seed_after=False):
        if not AVAILABLE:
            raise RuntimeError("libtorrent is not installed. " + INSTALL_HINT)
        self.url, self.out_dir, self.seed_after = url, out_dir, seed_after
        self.state = "queued"
        self.error = None
        self.filename = None
        self._h = None
        self._finished = False

    # --- progress, read live from libtorrent --------------------------------
    def _st(self):
        return self._h.status() if self._h else None

    @property
    def total(self):
        s = self._st()
        return int(s.total_wanted) if s else 0

    @property
    def downloaded(self):
        s = self._st()
        return int(s.total_wanted_done) if s else 0

    @property
    def percent(self):
        s = self._st()
        return float(s.progress) * 100 if s else 0.0

    @property
    def speed(self):
        s = self._st()
        return float(s.download_rate) if s and self.state == "running" else 0.0

    @property
    def eta(self):
        s = self._st()
        if s and s.download_rate > 0 and s.total_wanted:
            return (s.total_wanted - s.total_wanted_done) / s.download_rate
        return None

    @property
    def path(self):
        s = self._st()
        return os.path.join(self.out_dir, s.name) if s and s.name else None

    @property
    def peers(self):
        s = self._st()
        return int(s.num_peers) if s else 0

    def _tick(self):
        """Called by the scheduler: syncs state with libtorrent."""
        s = self._st()
        if not s or self.state != "running":
            return
        if s.errc.value():
            self.state, self.error = "error", s.errc.message()
        elif s.is_finished or s.state in (lt.torrent_status.seeding,
                                          lt.torrent_status.finished):
            self.state = "done"
            if not self.seed_after:
                self._h.pause()

    def start(self):
        os.makedirs(self.out_dir, exist_ok=True)
        self.error = None
        if self._h is None:
            u = self.url.strip()
            if u.startswith("magnet:"):
                params = lt.parse_magnet_uri(u)
            else:
                with urllib.request.urlopen(
                        urllib.request.Request(u, headers={"User-Agent": "DivineIDM/1.0"}),
                        timeout=30) as r:
                    data = r.read()
                params = lt.add_torrent_params()
                params.ti = lt.torrent_info(lt.bdecode(data))
            params.save_path = self.out_dir
            self._h = _ses().add_torrent(params)
        else:
            self._h.resume()
        self.state = "running"

    def pause(self):
        if self._h and self.state == "running":
            self._h.pause()
            self.state = "paused"

    def remove(self, delete_files=False):
        if self._h:
            _ses().remove_torrent(self._h, 1 if delete_files else 0)
            self._h = None
