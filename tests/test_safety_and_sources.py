"""The two filters that decide what Jarvis is allowed to learn from."""

from __future__ import annotations

import pytest

from jarvis.learning.sources import (
    SourceCandidate,
    SourceRegistry,
    TraderVetting,
    _mini_yaml,
)
from jarvis.safety.url_safety import URLSafetyChecker, registrable_domain


# ------------------------------------------------------------------- URL gate
@pytest.fixture
def checker() -> URLSafetyChecker:
    return URLSafetyChecker()


@pytest.mark.parametrize(
    "url",
    [
        "https://finance.yahoo.com/quote/AAPL",
        "https://www.sec.gov/edgar/search/",
        "https://www.reuters.com/markets/",
    ],
)
def test_allowlisted_domains_are_safe(checker, url):
    assert checker.check(url).is_safe


@pytest.mark.parametrize(
    "url,expected_reason",
    [
        ("http://finance.yahoo.com/quote/AAPL", "https"),
        ("https://bit.ly/abc123", "shortener"),
        ("https://192.168.0.5/panel", "IP address"),
        ("https://xn--yaho-sqa.com/", "punycode"),
        ("https://user:pass@finance.yahoo.com/", "credentials"),
        ("https://reuters.com/file.zip", "binary"),
        ("https://yahoo-secure-login.com/verify-account", "scam pattern"),
        ("https://freesignals.example.com/free-signals-now", "scam pattern"),
    ],
)
def test_dangerous_urls_are_blocked(checker, url, expected_reason):
    verdict = checker.check(url)
    assert verdict.verdict == "unsafe"
    assert any(expected_reason in reason for reason in verdict.reasons)


def test_unknown_domains_are_not_followed(checker):
    """Not provably bad is still not 'safe' -- Jarvis does not open it."""
    verdict = checker.check("https://some-random-trading-blog.example/post")
    assert verdict.verdict == "unknown"
    assert not verdict.is_safe


def test_brand_lookalikes_are_rejected(checker):
    assert checker.check("https://tradingview-premium.net/signals").verdict == "unsafe"
    assert checker.check("https://coinbase-wallet-verify.io/").verdict == "unsafe"


def test_filter_safe_keeps_only_openable_links(checker):
    urls = [
        "https://finance.yahoo.com/a",
        "https://bit.ly/x",
        "https://unknown-blog.example/b",
        "https://www.sec.gov/c",
    ]
    assert checker.filter_safe(urls) == [
        "https://finance.yahoo.com/a",
        "https://www.sec.gov/c",
    ]


def test_require_safe_browsing_blocks_everything_without_a_key():
    strict = URLSafetyChecker(require_safe_browsing=True, safe_browsing_key=None)
    verdict = strict.check("https://finance.yahoo.com/quote/AAPL")
    assert verdict.verdict == "unknown"
    assert not verdict.is_safe


def test_safe_browsing_hit_overrides_the_allowlist(monkeypatch):
    strict = URLSafetyChecker(safe_browsing_key="test-key")
    monkeypatch.setattr(
        strict, "_safe_browsing", lambda url: ("unsafe", "flagged as MALWARE")
    )
    verdict = strict.check("https://finance.yahoo.com/quote/AAPL")
    assert verdict.verdict == "unsafe"


def test_safe_browsing_clears_a_non_allowlisted_domain(monkeypatch):
    checker = URLSafetyChecker(safe_browsing_key="test-key")
    monkeypatch.setattr(checker, "_safe_browsing", lambda url: ("safe", "no match"))
    assert checker.check("https://good-research-site.example/report").is_safe


@pytest.mark.parametrize(
    "host,expected",
    [
        ("finance.yahoo.com", "yahoo.com"),
        ("www.bbc.co.uk", "bbc.co.uk"),
        ("sec.gov", "sec.gov"),
        ("a.b.c.example.com", "example.com"),
    ],
)
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected


# -------------------------------------------------------------- trader vetting
@pytest.fixture
def vetting() -> TraderVetting:
    return TraderVetting()


def test_strong_evidence_gets_verified(vetting):
    result = vetting.vet(
        SourceCandidate(
            platform="youtube",
            handle="@proven",
            evidence=["regulatory_registration", "audited_track_record"],
            tenure_years=9,
        )
    )
    assert result.verification == "verified"
    assert result.credibility > 0.55


def test_thin_evidence_is_provisional_not_verified(vetting):
    result = vetting.vet(
        SourceCandidate(
            platform="youtube",
            handle="@maybe",
            evidence=["platform_verified", "peer_endorsement"],
            tenure_years=6,
        )
    )
    assert result.verification == "provisional"
    assert result.credibility < 0.5


def test_a_verification_badge_alone_is_not_enough(vetting):
    result = vetting.vet(
        SourceCandidate(platform="youtube", handle="@badge", evidence=["platform_verified"])
    )
    assert result.verification == "rejected"


def test_no_evidence_is_rejected(vetting):
    result = vetting.vet(
        SourceCandidate(platform="tiktok", handle="@random", followers=2_000_000)
    )
    assert result.verification == "rejected"
    assert not result.accepted


def test_audience_alone_cannot_buy_verification(vetting):
    """Ten million followers is worth 0.04 -- reach is not evidence of skill."""
    result = vetting.vet(
        SourceCandidate(platform="instagram", handle="@huge", followers=10_000_000)
    )
    assert result.verification == "rejected"


@pytest.mark.parametrize(
    "bio",
    [
        "I deliver guaranteed returns every single month",
        "Join my signals group and never lose a trade",
        "98% win rate, verified",
        "Copy my trades and make money risk-free profit",
        "Double your money in 30 days",
    ],
)
def test_scam_language_is_disqualifying_regardless_of_credentials(vetting, bio):
    result = vetting.vet(
        SourceCandidate(
            platform="youtube",
            handle="@guru",
            evidence=["regulatory_registration", "audited_track_record"],
            tenure_years=20,
            followers=5_000_000,
            bio=bio,
        )
    )
    assert result.verification == "rejected"
    assert "disqualified" in result.rationale[0]


def test_unsupported_platform_rejected(vetting):
    result = vetting.vet(
        SourceCandidate(platform="myspace", handle="@x", evidence=["audited_track_record"])
    )
    assert result.verification == "rejected"


def test_registry_only_stores_survivors(memory, tmp_path):
    curated = tmp_path / "sources.yaml"
    curated.write_text(
        """
youtube:
  - handle: "@legit"
    name: "Legit Desk"
    evidence:
      - regulatory_registration
      - audited_track_record
    tenure_years: 10
  - handle: "@nobody"
    name: "Random Guy"
    tenure_years: 1
  - handle: "@scammer"
    name: "Guru"
    evidence:
      - audited_track_record
      - regulatory_registration
    bio: "guaranteed returns every month"
""",
        encoding="utf-8",
    )
    registry = SourceRegistry(memory.knowledge)
    counts = registry.sync(curated)

    assert counts["verified"] == 1
    assert counts["rejected"] == 2
    handles = {s["handle"] for s in memory.knowledge.trusted_sources("youtube")}
    assert handles == {"@legit"}


def test_mini_yaml_parses_the_curated_shape():
    parsed = _mini_yaml(
        """
youtube:
  - handle: "@a"
    tenure_years: 7
    evidence:
      - long_tenure
      - platform_verified
"""
    )
    entry = parsed["youtube"][0]
    assert entry["handle"] == "@a"
    assert entry["tenure_years"] == 7
    assert entry["evidence"] == ["long_tenure", "platform_verified"]
