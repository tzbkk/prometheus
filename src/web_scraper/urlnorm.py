"""URL normalization for QQ CDN media URLs.

QQ video CDN URLs include per-request auth params (``dis_k``, ``dis_t``)
that change every API response. Without normalization, the same video gets
a different SHA256 hash each time → re-downloaded and stored under a new
filename.  This module strips those volatile params so the hash is stable.
"""

from urllib.parse import urlparse, parse_qs, urlencode

_VOLATILE_PARAMS = frozenset({"dis_k", "dis_t"})


def normalize_media_url(url: str) -> str:
    """Strip volatile CDN auth params from a media URL.

    >>> normalize_media_url("https://x.qq.com/v.mp4?dis_k=abc&dis_t=123")
    'https://x.qq.com/v.mp4'
    >>> normalize_media_url("https://x.qq.com/v.mp4?dis_k=abc&keep=1&dis_t=123")
    'https://x.qq.com/v.mp4?keep=1'
    >>> normalize_media_url("https://x.qq.com/img.jpg")
    'https://x.qq.com/img.jpg'
    """
    if not url:
        return url
    parsed = urlparse(url)
    if not parsed.query:
        return url
    qs = parse_qs(parsed.query, keep_blank_values=True)
    cleaned = {k: v for k, v in qs.items() if k not in _VOLATILE_PARAMS}
    if len(cleaned) == len(qs):
        return url
    new_query = urlencode(cleaned, doseq=True)
    return parsed._replace(query=new_query).geturl()
