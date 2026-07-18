"""Tests for MediaDownloader.download_comment_media + CommentsScraper wiring.

All network access is mocked — no real HTTP calls are made.
"""

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.web_scraper.media import MediaDownloader
from src.web_scraper.comments import CommentsScraper


def _mock_urlopen(data: bytes = b"\xff\xd8\xff\xe0fake_image_data"):
    """Context-manager-shaped MagicMock matching the pattern in test_media.py."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    cm.read.return_value = data
    cm.status = 200
    return cm


def _expected_filename(url: str) -> str:
    """SHA256 filename for a URL (image extension default; matches _guess_ext)."""
    return hashlib.sha256(url.encode()).hexdigest()[:16] + ".jpg"


def _read_comment_index(dl: MediaDownloader) -> list[dict]:
    """Read comment_media_index.jsonl entries as a list of dicts."""
    if not dl._comment_index_path.exists():
        return []
    return [
        json.loads(line)
        for line in dl._comment_index_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _make_comment_with_image(cid="C_abc", url="https://cdn.example.com/c1.jpg"):
    return {
        "id": cid,
        "richContents": {
            "images": [{"picUrl": url, "width": 800, "height": 600, "is_gif": False}]
        },
    }


def _make_comment_with_sticker(cid="C_stk", url="https://cdn.example.com/stk.png"):
    return {
        "id": cid,
        "richContents": {
            "sticker": {
                "custom_face": {
                    "origin_image_url": url,
                    "pic_width": 200,
                    "pic_height": 200,
                }
            }
        },
    }


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_download_comment_media_extracts_images(mock_urlopen, tmp_path):
    mock_urlopen.return_value = _mock_urlopen()
    dl = MediaDownloader(tmp_path)
    url = "https://cdn.example.com/c1.jpg"
    comment = _make_comment_with_image(url=url)

    dl.download_comment_media(comment, feed_id="B_test")

    expected = _expected_filename(url)
    assert (dl.media_dir / expected).exists()
    entries = _read_comment_index(dl)
    assert len(entries) == 1
    assert entries[0]["comment_id"] == "C_abc"
    assert entries[0]["type"] == "image"
    assert entries[0]["file"] == expected


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_download_comment_media_extracts_stickers(mock_urlopen, tmp_path):
    mock_urlopen.return_value = _mock_urlopen()
    dl = MediaDownloader(tmp_path)
    url = "https://cdn.example.com/stk.png"
    comment = _make_comment_with_sticker(url=url)

    dl.download_comment_media(comment, feed_id="B_test")

    expected = _expected_filename(url)
    assert (dl.media_dir / expected).exists()
    entries = _read_comment_index(dl)
    assert len(entries) == 1
    assert entries[0]["type"] == "sticker"
    assert entries[0]["file"] == expected


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_download_comment_media_recurses_replies(mock_urlopen, tmp_path):
    mock_urlopen.return_value = _mock_urlopen()
    dl = MediaDownloader(tmp_path)
    parent_url = "https://cdn.example.com/parent.jpg"
    reply_url = "https://cdn.example.com/reply.jpg"
    comment = {
        "id": "C_parent",
        "richContents": {
            "images": [{"picUrl": parent_url, "width": 10, "height": 10}]
        },
        "vecReply": [
            {
                "id": "C_reply1",
                "richContents": {
                    "images": [{"picUrl": reply_url, "width": 5, "height": 5}]
                },
            }
        ],
    }

    count = dl.download_comment_media(comment, feed_id="B_test")

    assert count == 2
    assert (dl.media_dir / _expected_filename(parent_url)).exists()
    assert (dl.media_dir / _expected_filename(reply_url)).exists()
    entries = _read_comment_index(dl)
    assert len(entries) == 2
    cids = {e["comment_id"] for e in entries}
    assert cids == {"C_parent", "C_reply1"}


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_download_comment_media_dedup_with_feed(mock_urlopen, tmp_path):
    """A URL downloaded for a feed must NOT be re-fetched for a comment,
    but the comment_media_index.jsonl mapping must STILL be written."""
    shared_url = "https://cdn.example.com/shared.jpg"
    mock_urlopen.return_value = _mock_urlopen()
    dl = MediaDownloader(tmp_path)

    feed = {"id": "B_test", "images": [{"picUrl": shared_url}]}
    feed_count = dl.download_feed_media(feed)
    assert feed_count == 1
    assert mock_urlopen.call_count == 1
    assert (dl.media_dir / _expected_filename(shared_url)).exists()

    comment = _make_comment_with_image(url=shared_url)
    dl.download_comment_media(comment, feed_id="B_test")

    assert mock_urlopen.call_count == 1
    assert len(list(dl.media_dir.iterdir())) == 1
    entries = _read_comment_index(dl)
    assert len(entries) == 1
    assert entries[0]["file"] == _expected_filename(shared_url)
    feed_lines = dl._index_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(feed_lines) == 1
    assert json.loads(feed_lines[0])["source"] == "B_test"


@patch("src.web_scraper.media.urllib.request.urlopen")
def test_comment_media_index_format(mock_urlopen, tmp_path):
    mock_urlopen.return_value = _mock_urlopen()
    dl = MediaDownloader(tmp_path)
    url = "https://cdn.example.com/fmt.jpg"
    comment = _make_comment_with_image(cid="C_fmt", url=url)

    dl.download_comment_media(comment, feed_id="B_format")

    entries = _read_comment_index(dl)
    assert len(entries) == 1
    for key in ("comment_id", "feed_id", "url", "file", "type", "width", "height", "size"):
        assert key in entries[0], f"missing key: {key}"
    assert entries[0]["comment_id"] == "C_fmt"
    assert entries[0]["feed_id"] == "B_format"
    assert entries[0]["url"] == url
    assert entries[0]["file"] == _expected_filename(url)
    assert entries[0]["type"] == "image"
    assert entries[0]["width"] == 800
    assert entries[0]["height"] == 600
    assert entries[0]["size"] > 0


def _make_client_single_page(comment_list):
    """Mock client returning one comment page then empty."""
    client = MagicMock()
    states = [{"page": 0}]

    def get_feed_comments(feed_id, attch_info=""):
        if states[0]["page"] == 0:
            states[0]["page"] = 1
            return (comment_list, len(comment_list), "")
        return ([], 0, "")

    client.get_feed_comments.side_effect = get_feed_comments
    return client


def _make_store():
    store = MagicMock()
    store.append_comment.side_effect = lambda r: True
    return store


def test_comments_scraper_wires_media_downloader():
    """When media_downloader is provided, download_comment_media is called
    with each comment dict + the correct feed_id."""
    comment = _make_comment_with_image(cid="C_wire", url="https://cdn.example.com/wire.jpg")
    client = _make_client_single_page([comment])
    store = _make_store()
    media = MagicMock()
    media.download_comment_media.return_value = 1
    scraper = CommentsScraper(
        client, store, "guild123", media_downloader=media
    )

    scraper.scrape_feed_comments("B_wire")

    media.download_comment_media.assert_called_once_with(comment, feed_id="B_wire")


def test_comments_scraper_backward_compat_no_downloader():
    """Without media_downloader, scrape_feed_comments works exactly as before
    (no download attempt, no exception)."""
    comment = _make_comment_with_image(cid="C_legacy")
    client = _make_client_single_page([comment])
    store = _make_store()
    scraper = CommentsScraper(client, store, "guild123")

    result = scraper.scrape_feed_comments("B_legacy")

    assert result == 1
    assert client.get_feed_comments.call_count == 1
    assert store.append_comment.call_count == 1
