"""URL router / classifier: decides which family of engines handles a link."""
from urllib.parse import urlparse

from utils import probe_url

# Video/audio platforms -> media chain (yt-dlp, then pytube/you-get/lux/gallery-dl fallbacks)
MEDIA_HOSTS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com", "twitter.com", "x.com",
    "instagram.com", "tiktok.com", "facebook.com", "fb.watch", "vimeo.com",
    "dailymotion.com", "twitch.tv", "reddit.com", "soundcloud.com",
    "streamable.com", "bandcamp.com",
    # Chinese platforms that You-Get / Lux handle well
    "bilibili.com", "b23.tv", "youku.com", "iqiyi.com", "qq.com", "weibo.com",
    "xiaohongshu.com", "ixigua.com", "douyin.com", "acfun.cn",
)
# Image-gallery sites -> gallery-dl first
GALLERY_HOSTS = (
    "pinterest.com", "imgur.com", "flickr.com", "deviantart.com", "artstation.com",
    "pixiv.net", "tumblr.com", "behance.net", "500px.com", "unsplash.com",
    "danbooru.donmai.us", "bsky.app",
)


def host_of(url):
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _match(host, hosts):
    return any(host == h or host.endswith("." + h) for h in hosts)


def is_youtube(url):
    return _match(host_of(url), ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def classify(url):
    """Return (route, probe_info): route is "gallery", "media" or "direct"."""
    host = host_of(url)
    if _match(host, GALLERY_HOSTS):
        return "gallery", None
    if _match(host, MEDIA_HOSTS):
        return "media", None
    info = probe_url(url)
    # An HTML page isn't a file: let the media chain look for something inside it
    if info["content_type"] in ("text/html", "application/xhtml+xml"):
        return "media", info
    return "direct", info
