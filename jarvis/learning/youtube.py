"""Studying curated YouTube traders.

Two optional dependencies do the work:

``youtube-transcript-api``  pulls the spoken transcript of a video.
``YOUTUBE_API_KEY``         lists a curated channel's recent uploads.

Neither is required. Without them the ingestor simply reports that it could not
fetch anything, and the rest of the learning loop carries on with what it has.
Only channels that survive vetting in ``sources.py`` are ever queried.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"


@dataclass
class Video:
    video_id: str
    title: str
    channel_id: str
    channel_title: str
    published_at: str
    url: str


class YouTubeStudy:
    def __init__(self, api_key: str | None = None, *, timeout: int = 20) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.last_error: str | None = None

    # ------------------------------------------------------------ discovery
    def _get(self, endpoint: str, params: dict[str, Any]) -> dict | None:
        if not self.api_key:
            self.last_error = "no YOUTUBE_API_KEY configured"
            return None
        params = {**params, "key": self.api_key}
        url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:
            self.last_error = str(exc)
            log.debug("youtube api %s failed: %s", endpoint, exc)
            return None

    def resolve_channel_id(self, handle: str) -> str | None:
        """Accept a raw channel id, an @handle, or a legacy username."""
        if handle.startswith("UC") and len(handle) == 24:
            return handle
        query = handle.lstrip("@")
        body = self._get("search", {"part": "snippet", "q": query, "type": "channel", "maxResults": 1})
        items = (body or {}).get("items") or []
        if not items:
            return None
        return items[0].get("snippet", {}).get("channelId") or items[0].get("id", {}).get("channelId")

    def recent_videos(self, channel_handle: str, limit: int = 5) -> list[Video]:
        channel_id = self.resolve_channel_id(channel_handle)
        if not channel_id:
            return []
        body = self._get(
            "search",
            {
                "part": "snippet",
                "channelId": channel_id,
                "order": "date",
                "type": "video",
                "maxResults": max(1, min(limit, 25)),
            },
        )
        videos: list[Video] = []
        for item in (body or {}).get("items", []):
            vid = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if not vid:
                continue
            videos.append(
                Video(
                    video_id=vid,
                    title=snippet.get("title", ""),
                    channel_id=channel_id,
                    channel_title=snippet.get("channelTitle", channel_handle),
                    published_at=snippet.get("publishedAt", ""),
                    url=f"https://www.youtube.com/watch?v={vid}",
                )
            )
        return videos

    # ----------------------------------------------------------- transcript
    def transcript(self, video_id: str, languages: Sequence[str] = ("en",)) -> str | None:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
        except ImportError:
            self.last_error = "youtube-transcript-api not installed"
            return None
        try:
            # The library's API changed shape across major versions; support both.
            if hasattr(YouTubeTranscriptApi, "get_transcript"):
                chunks = YouTubeTranscriptApi.get_transcript(video_id, languages=list(languages))
            else:
                fetched = YouTubeTranscriptApi().fetch(video_id, languages=list(languages))
                chunks = getattr(fetched, "to_raw_data", lambda: fetched)()
        except Exception as exc:
            self.last_error = f"transcript unavailable for {video_id}: {exc}"
            log.debug(self.last_error)
            return None
        parts = [
            (c.get("text") if isinstance(c, dict) else getattr(c, "text", ""))
            for c in chunks
        ]
        text = " ".join(p for p in parts if p and p not in {"[Music]", "[Applause]"})
        return text.strip() or None

    # ------------------------------------------------------------- pipeline
    def study_sources(
        self,
        knowledge_store,
        *,
        per_channel: int = 3,
        max_channels: int = 10,
    ) -> dict[str, int]:
        """Fetch new videos from every verified YouTube teacher and store them."""
        stats = {"channels": 0, "videos_seen": 0, "stored": 0, "no_transcript": 0}
        for source in knowledge_store.trusted_sources("youtube")[:max_channels]:
            stats["channels"] += 1
            for video in self.recent_videos(source["handle"], per_channel):
                stats["videos_seen"] += 1
                text = self.transcript(video.video_id)
                if not text:
                    stats["no_transcript"] += 1
                    continue
                content_id = knowledge_store.add_content(
                    platform="youtube",
                    external_id=video.video_id,
                    source_id=source["id"],
                    url=video.url,
                    title=video.title,
                    published_at=video.published_at,
                    body=text,
                )
                if content_id:
                    stats["stored"] += 1
        return stats
