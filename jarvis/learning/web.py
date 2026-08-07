"""Web research: Google search, then only ever opening links that clear safety.

Search goes through Google's Programmable Search Engine API (the supported way
to query Google programmatically -- set ``GOOGLE_CSE_KEY`` and
``GOOGLE_CSE_ID``). Every result URL is put through ``URLSafetyChecker`` before
a single byte is fetched; anything that comes back ``unsafe`` or ``unknown`` is
dropped and recorded, never opened.
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..safety.url_safety import URLSafetyChecker, Verdict

log = logging.getLogger(__name__)

CSE_URL = "https://www.googleapis.com/customsearch/v1?{query}"

_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    verdict: Verdict | None = None


@dataclass
class FetchedPage:
    url: str
    title: str
    text: str


@dataclass
class ResearchReport:
    query: str
    opened: list[FetchedPage] = field(default_factory=list)
    blocked: list[tuple[str, str]] = field(default_factory=list)  # (url, reason)
    error: str | None = None


class WebResearcher:
    def __init__(
        self,
        safety: URLSafetyChecker,
        *,
        cse_key: str | None = None,
        cse_id: str | None = None,
        timeout: int = 20,
        max_bytes: int = 800_000,
    ) -> None:
        self.safety = safety
        self.cse_key = cse_key
        self.cse_id = cse_id
        self.timeout = timeout
        self.max_bytes = max_bytes

    # -------------------------------------------------------------- search
    def search(self, query: str, *, limit: int = 8) -> tuple[list[SearchResult], str | None]:
        if not (self.cse_key and self.cse_id):
            return [], (
                "Google search needs GOOGLE_CSE_KEY and GOOGLE_CSE_ID "
                "(Programmable Search Engine)"
            )
        params = urllib.parse.urlencode(
            {"key": self.cse_key, "cx": self.cse_id, "q": query, "num": min(limit, 10)}
        )
        try:
            with urllib.request.urlopen(CSE_URL.format(query=params), timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except Exception as exc:
            log.debug("google search failed: %s", exc)
            return [], f"search failed: {exc}"
        results = [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
            )
            for item in body.get("items", [])
            if item.get("link")
        ]
        return results, None

    # --------------------------------------------------------------- fetch
    def fetch(self, url: str) -> FetchedPage | None:
        """Fetch a page. Refuses outright unless the safety gate says 'safe'."""
        verdict = self.safety.check(url)
        if not verdict.is_safe:
            log.info("refusing to open %s -- %s", url, verdict)
            return None
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Jarvis/1.0; research)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                ctype = resp.headers.get("Content-Type", "")
                if "html" not in ctype and "text" not in ctype:
                    return None
                raw = resp.read(self.max_bytes)
        except Exception as exc:
            log.debug("fetch failed for %s: %s", url, exc)
            return None
        charset = "utf-8"
        match = re.search(r"charset=([\w-]+)", ctype, re.I)
        if match:
            charset = match.group(1)
        markup = raw.decode(charset, errors="replace")
        return FetchedPage(url=url, title=_extract_title(markup), text=extract_text(markup))

    # ------------------------------------------------------------ research
    def research(self, query: str, *, max_pages: int = 3) -> ResearchReport:
        """Search, filter through safety, and read only what passed."""
        report = ResearchReport(query=query)
        results, error = self.search(query)
        if error:
            report.error = error
            return report
        for result in results:
            if len(report.opened) >= max_pages:
                break
            verdict = self.safety.check(result.url)
            result.verdict = verdict
            if not verdict.is_safe:
                report.blocked.append((result.url, str(verdict)))
                continue
            page = self.fetch(result.url)
            if page and len(page.text) > 200:
                report.opened.append(page)
        return report

    def study(self, knowledge_store, query: str, *, max_pages: int = 3) -> ResearchReport:
        """Research a topic and file what was read for the distiller."""
        report = self.research(query, max_pages=max_pages)
        for page in report.opened:
            knowledge_store.add_content(
                platform="web",
                external_id=page.url,
                url=page.url,
                title=page.title or query,
                body=page.text,
            )
        return report


def extract_text(markup: str) -> str:
    """Strip markup down to readable prose without pulling in a parser dep."""
    body = _SCRIPT_STYLE.sub(" ", markup)
    body = re.sub(r"<(p|br|div|li|h[1-6]|tr)[^>]*>", "\n", body, flags=re.I)
    body = _TAGS.sub(" ", body)
    body = html.unescape(body)
    body = _WS.sub(" ", body)
    body = "\n".join(line.strip() for line in body.splitlines())
    return _BLANK_LINES.sub("\n\n", body).strip()


def _extract_title(markup: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", markup, re.I | re.S)
    return html.unescape(_WS.sub(" ", match.group(1)).strip()) if match else ""
