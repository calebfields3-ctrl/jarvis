"""The gate every URL must pass before Jarvis will open it.

A link is only opened when it clears *all* of:

1. Scheme is HTTPS, host is a real registrable domain (no IP literals, no
   embedded credentials, no homograph punycode).
2. It survives the heuristic screen (shorteners, lookalike domains, risky
   file types, scam-shaped paths).
3. It is reputationally cleared -- either the host is on the curated
   allowlist of institutions Jarvis is allowed to study, or Google Safe
   Browsing returns no threat match.

Anything that is merely "probably fine" comes back ``unknown`` and is not
followed. Verdicts are cached in memory so a domain is judged once per window.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable

log = logging.getLogger(__name__)

SAFE_BROWSING_URL = (
    "https://safebrowsing.googleapis.com/v4/threatMatches:find?key={key}"
)

# Domains Jarvis is allowed to read directly. Deliberately conservative: these
# are exchanges, regulators, primary financial data and established newsrooms.
ALLOWLIST: frozenset[str] = frozenset(
    {
        # market data & fundamentals
        "finance.yahoo.com", "yahoo.com", "tradingview.com", "nasdaq.com",
        "nyse.com", "cboe.com", "cmegroup.com", "marketwatch.com",
        "morningstar.com", "stockanalysis.com", "macrotrends.net",
        # regulators & primary filings
        "sec.gov", "federalreserve.gov", "finra.org", "cftc.gov",
        "bls.gov", "bea.gov", "treasury.gov", "ecb.europa.eu",
        "bankofengland.co.uk", "imf.org", "worldbank.org",
        # newsrooms
        "reuters.com", "apnews.com", "bloomberg.com", "wsj.com", "ft.com",
        "cnbc.com", "bbc.com", "bbc.co.uk", "npr.org", "economist.com",
        "barrons.com", "investors.com", "axios.com", "theguardian.com",
        # education / reference
        "investopedia.com", "cfainstitute.org", "khanacademy.org",
        # curated video platforms (channel-level curation happens elsewhere)
        "youtube.com", "youtu.be",
    }
)

# Never followed, whatever else says.
BLOCKLIST_SUFFIXES: tuple[str, ...] = (
    ".zip", ".exe", ".dmg", ".apk", ".msi", ".scr", ".jar", ".bat", ".sh",
    ".iso", ".rar", ".7z",
)

SHORTENERS: frozenset[str] = frozenset(
    {
        "bit.ly", "tinyurl.com", "goo.gl", "t.co", "ow.ly", "buff.ly",
        "is.gd", "cutt.ly", "rebrand.ly", "shorturl.at", "rb.gy", "lnkd.in",
    }
)

# Paths/queries that read like a scam pitch rather than market research.
SCAM_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"guaranteed[-_ ]?(returns?|profits?)",
        r"double[-_ ]your[-_ ](money|invest)",
        r"free[-_ ]?(signals?|money|crypto|airdrop)",
        r"(pump|whale)[-_ ]?(group|signal|alert)",
        r"copy[-_ ]?trade[-_ ]?bot",
        r"(connect|verify|validate)[-_ ]?wallet",
        r"seed[-_ ]?phrase",
        r"(login|signin|verify)[-_ ]?(account|now)",
        r"binary[-_ ]options?",
        r"risk[-_ ]?free[-_ ]?(trading|profit)",
    )
)

# Brands commonly impersonated; a host that *contains* one without *being* one
# is a lookalike.
IMPERSONATION_TARGETS: tuple[str, ...] = (
    "yahoo", "tradingview", "bloomberg", "reuters", "nasdaq", "schwab",
    "fidelity", "robinhood", "coinbase", "binance", "etrade", "vanguard",
    "interactivebrokers", "metamask",
)

_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


@dataclass
class Verdict:
    url: str
    verdict: str                     # 'safe' | 'unsafe' | 'unknown'
    reasons: list[str] = field(default_factory=list)
    checked_by: str = "heuristics"

    @property
    def is_safe(self) -> bool:
        return self.verdict == "safe"

    def __str__(self) -> str:
        return f"{self.verdict}: {'; '.join(self.reasons) or 'no findings'}"


def registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 without pulling in a public-suffix dependency.

    Handles the common two-part suffixes Jarvis actually meets (co.uk, com.au).
    """
    host = host.lower().strip(".")
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    two_part_suffixes = {
        "co.uk", "com.au", "co.jp", "co.in", "com.br", "co.nz", "com.hk",
        "com.sg", "co.za", "com.mx", "org.uk", "gov.uk", "ac.uk", "europa.eu",
    }
    last_two = ".".join(parts[-2:])
    if last_two in two_part_suffixes:
        return ".".join(parts[-3:])
    return last_two


class URLSafetyChecker:
    """Heuristics plus optional Google Safe Browsing, with a verdict cache."""

    def __init__(
        self,
        *,
        safe_browsing_key: str | None = None,
        require_safe_browsing: bool = False,
        allowlist: Iterable[str] | None = None,
        timeout: int = 15,
    ) -> None:
        self.safe_browsing_key = safe_browsing_key
        # When True, allowlist membership alone is not enough -- Safe Browsing
        # must also clear the URL, and no key means nothing is followed.
        self.require_safe_browsing = require_safe_browsing
        self.allowlist = set(allowlist) if allowlist is not None else set(ALLOWLIST)
        self.timeout = timeout
        self._cache: dict[str, Verdict] = {}

    # ---------------------------------------------------------------- api
    def check(self, url: str) -> Verdict:
        cached = self._cache.get(url)
        if cached:
            return cached
        verdict = self._check_uncached(url)
        self._cache[url] = verdict
        return verdict

    def filter_safe(self, urls: Iterable[str]) -> list[str]:
        """Keep only the links Jarvis is allowed to open."""
        return [u for u in urls if self.check(u).is_safe]

    def allow_domain(self, domain: str) -> None:
        self.allowlist.add(domain.lower())
        self._cache.clear()

    # ----------------------------------------------------------- internals
    def _check_uncached(self, url: str) -> Verdict:
        structural = self._structural_check(url)
        if structural.verdict == "unsafe":
            return structural

        parsed = urllib.parse.urlparse(url)
        domain = registrable_domain(parsed.hostname or "")
        allowlisted = domain in self.allowlist

        sb_verdict, sb_reason = self._safe_browsing(url)
        if sb_verdict == "unsafe":
            return Verdict(url, "unsafe", [sb_reason], "safe_browsing")

        if self.require_safe_browsing:
            if sb_verdict == "safe":
                return Verdict(
                    url, "safe",
                    ["cleared by Google Safe Browsing"]
                    + (["allowlisted domain"] if allowlisted else []),
                    "safe_browsing",
                )
            return Verdict(
                url, "unknown",
                ["Safe Browsing verification required but unavailable"],
                "policy",
            )

        if allowlisted:
            reasons = [f"{domain} is on the curated allowlist"]
            if sb_verdict == "safe":
                reasons.append("cleared by Google Safe Browsing")
            return Verdict(url, "safe", reasons, "allowlist")

        if sb_verdict == "safe":
            return Verdict(
                url, "safe",
                ["cleared by Google Safe Browsing", "clean structural screen"],
                "safe_browsing",
            )

        return Verdict(
            url, "unknown",
            [f"{domain or 'host'} is not allowlisted and could not be independently verified"],
            "heuristics",
        )

    def _structural_check(self, url: str) -> Verdict:
        reasons: list[str] = []
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return Verdict(url, "unsafe", ["malformed URL"])

        if parsed.scheme != "https":
            return Verdict(url, "unsafe", [f"scheme {parsed.scheme or 'missing'!r} is not https"])
        host = (parsed.hostname or "").lower()
        if not host or "." not in host:
            return Verdict(url, "unsafe", ["missing or non-public hostname"])
        if parsed.username or parsed.password:
            return Verdict(url, "unsafe", ["credentials embedded in the URL"])
        if _IPV4.match(host) or ":" in (parsed.netloc.split("@")[-1].rstrip("0123456789:")):
            if _IPV4.match(host):
                return Verdict(url, "unsafe", ["bare IP address instead of a domain"])
        if host.startswith("xn--") or ".xn--" in host:
            return Verdict(url, "unsafe", ["punycode host -- homograph risk"])
        if host.count(".") > 4:
            reasons.append("unusually deep subdomain nesting")

        domain = registrable_domain(host)
        if domain in SHORTENERS:
            return Verdict(url, "unsafe", ["URL shortener hides the real destination"])

        path_and_query = f"{parsed.path}?{parsed.query}".lower()
        if path_and_query.rstrip("?").endswith(BLOCKLIST_SUFFIXES):
            return Verdict(url, "unsafe", ["links directly to a downloadable binary"])
        for pattern in SCAM_PATTERNS:
            if pattern.search(url):
                return Verdict(
                    url, "unsafe", [f"URL matches a known scam pattern ({pattern.pattern})"]
                )

        # Impersonation: the brand name appears in the host, but the registrable
        # domain is not one we recognise as actually belonging to that brand.
        # `tradingview-premium.net` and `yahoo-finance-login.com` are exactly
        # this shape, so a hyphenated brand token is evidence *for* a lookalike,
        # not an exemption from the check.
        core = domain.rsplit(".", 1)[0]
        is_allowlisted = any(
            domain == allowed or domain.endswith("." + allowed)
            for allowed in self.allowlist
        )
        if not is_allowlisted:
            for brand in IMPERSONATION_TARGETS:
                if brand in host and core != brand:
                    return Verdict(
                        url, "unsafe",
                        [f"host impersonates {brand!r} without being its real domain"],
                    )

        return Verdict(url, "unknown", reasons or ["clean structural screen"])

    def _safe_browsing(self, url: str) -> tuple[str, str]:
        """Returns ('safe'|'unsafe'|'unavailable', reason)."""
        if not self.safe_browsing_key:
            return "unavailable", "no Safe Browsing API key configured"
        payload = {
            "client": {"clientId": "jarvis-trading-assistant", "clientVersion": "1.0"},
            "threatInfo": {
                "threatTypes": [
                    "MALWARE", "SOCIAL_ENGINEERING",
                    "UNWANTED_SOFTWARE", "POTENTIALLY_HARMFUL_APPLICATION",
                ],
                "platformTypes": ["ANY_PLATFORM"],
                "threatEntryTypes": ["URL"],
                "threatEntries": [{"url": url}],
            },
        }
        request = urllib.request.Request(
            SAFE_BROWSING_URL.format(key=self.safe_browsing_key),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except Exception as exc:
            log.debug("Safe Browsing lookup failed: %s", exc)
            return "unavailable", f"Safe Browsing lookup failed: {exc}"
        matches = body.get("matches") or []
        if matches:
            kinds = ", ".join(sorted({m.get("threatType", "THREAT") for m in matches}))
            return "unsafe", f"Google Safe Browsing flagged this URL ({kinds})"
        return "safe", "no Safe Browsing threat match"
