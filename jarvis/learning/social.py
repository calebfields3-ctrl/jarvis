"""Studying curated Instagram and TikTok traders.

A deliberate design note, because it constrains what this module does.

Neither Instagram nor TikTok offers a general read API for other people's
posts, and scraping their sites is against their terms of service and breaks
every few weeks anyway. So Jarvis does **not** scrape. It ingests curated
creators through three legitimate routes, in priority order:

1. **Official APIs** -- Instagram Graph API / TikTok Display API, if you have
   an access token for an account with the relevant permissions. Set
   ``INSTAGRAM_ACCESS_TOKEN`` / ``TIKTOK_ACCESS_TOKEN``.
2. **oEmbed** -- the public, sanctioned endpoint for a specific post URL.
   Returns the caption and author for a post you already have a link to.
3. **A local drop folder** -- ``~/.jarvis/inbox/{instagram,tiktok}/*.txt|*.json``.
   Save a caption or your own data export there and Jarvis studies it on the
   next cycle. This is the route that always works.

Whichever route supplies it, the creator must already be a verified source, and
the content goes through the same distiller as everything else.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

OEMBED = {
    "instagram": "https://graph.facebook.com/v18.0/instagram_oembed?url={url}&access_token={token}",
    "tiktok": "https://www.tiktok.com/oembed?url={url}",
}


@dataclass
class SocialPost:
    platform: str
    external_id: str
    author: str
    caption: str
    url: str | None = None
    published_at: str | None = None


class SocialStudy:
    """Ingests short-form content from curated creators, without scraping."""

    def __init__(
        self,
        inbox_dir: Path | str,
        *,
        instagram_token: str | None = None,
        tiktok_token: str | None = None,
        timeout: int = 20,
    ) -> None:
        self.inbox = Path(inbox_dir)
        self.instagram_token = instagram_token or os.environ.get("INSTAGRAM_ACCESS_TOKEN")
        self.tiktok_token = tiktok_token or os.environ.get("TIKTOK_ACCESS_TOKEN")
        self.timeout = timeout
        self.notes: list[str] = []
        for platform in ("instagram", "tiktok"):
            (self.inbox / platform).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- oEmbed
    def fetch_post(self, platform: str, url: str) -> SocialPost | None:
        """Read one post through its platform's sanctioned oEmbed endpoint."""
        template = OEMBED.get(platform)
        if not template:
            return None
        if platform == "instagram" and not self.instagram_token:
            self.notes.append("instagram oEmbed needs INSTAGRAM_ACCESS_TOKEN")
            return None
        endpoint = template.format(
            url=urllib.parse.quote(url, safe=""),
            token=self.instagram_token or "",
        )
        try:
            with urllib.request.urlopen(endpoint, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except Exception as exc:
            log.debug("%s oEmbed failed for %s: %s", platform, url, exc)
            self.notes.append(f"{platform} oEmbed failed: {exc}")
            return None
        caption = body.get("title") or body.get("caption") or ""
        author = body.get("author_name") or body.get("author_unique_id") or ""
        return SocialPost(
            platform=platform,
            external_id=_post_id(url),
            author=author,
            caption=caption,
            url=url,
        )

    # -------------------------------------------------------- official API
    def instagram_user_media(self, limit: int = 10) -> list[SocialPost]:
        """Media for the token's own account (what the Graph API actually allows)."""
        if not self.instagram_token:
            return []
        url = (
            "https://graph.instagram.com/me/media"
            f"?fields=id,caption,permalink,timestamp&limit={limit}"
            f"&access_token={self.instagram_token}"
        )
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except Exception as exc:
            log.debug("instagram graph failed: %s", exc)
            return []
        return [
            SocialPost(
                platform="instagram",
                external_id=item.get("id", ""),
                author="self",
                caption=item.get("caption") or "",
                url=item.get("permalink"),
                published_at=item.get("timestamp"),
            )
            for item in body.get("data", [])
            if item.get("caption")
        ]

    # ---------------------------------------------------------- drop folder
    def read_inbox(self, platform: str) -> list[SocialPost]:
        """Pick up anything dropped in ``~/.jarvis/inbox/<platform>/``.

        ``.txt`` -- first line ``@author``, rest is the caption/transcript.
        ``.json`` -- a post object, or a list of them (e.g. a data export).
        """
        folder = self.inbox / platform
        posts: list[SocialPost] = []
        if not folder.exists():
            return posts
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() == ".txt":
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                if not lines:
                    continue
                author = lines[0].strip().lstrip("@") if lines[0].startswith("@") else path.stem
                body = "\n".join(lines[1:] if lines[0].startswith("@") else lines)
                if body.strip():
                    posts.append(SocialPost(platform, path.stem, author, body.strip()))
            elif path.suffix.lower() == ".json":
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    log.warning("skipping malformed %s: %s", path, exc)
                    continue
                for item in data if isinstance(data, list) else [data]:
                    if not isinstance(item, dict):
                        continue
                    caption = item.get("caption") or item.get("text") or item.get("description")
                    if not caption:
                        continue
                    posts.append(
                        SocialPost(
                            platform=platform,
                            external_id=str(item.get("id") or f"{path.stem}:{len(posts)}"),
                            author=str(item.get("author") or item.get("username") or path.stem),
                            caption=str(caption),
                            url=item.get("url") or item.get("permalink"),
                            published_at=item.get("timestamp") or item.get("published_at"),
                        )
                    )
        return posts

    # -------------------------------------------------------------- pipeline
    def study_sources(self, knowledge_store, *, platforms=("instagram", "tiktok")) -> dict[str, int]:
        """Ingest everything available, keeping only verified creators."""
        stats = {"seen": 0, "stored": 0, "skipped_unverified": 0}
        for platform in platforms:
            trusted = {
                s["handle"].lower().lstrip("@"): s
                for s in knowledge_store.trusted_sources(platform)
            }
            posts = self.read_inbox(platform)
            if platform == "instagram":
                posts += self.instagram_user_media()
            for post in posts:
                stats["seen"] += 1
                source = trusted.get(post.author.lower().lstrip("@"))
                if source is None:
                    stats["skipped_unverified"] += 1
                    continue
                content_id = knowledge_store.add_content(
                    platform=platform,
                    external_id=post.external_id,
                    source_id=source["id"],
                    url=post.url,
                    title=post.caption[:120],
                    published_at=post.published_at,
                    body=post.caption,
                )
                if content_id:
                    stats["stored"] += 1
        return stats


def _post_id(url: str) -> str:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or url
