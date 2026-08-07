"""Runtime configuration for Jarvis.

Everything has a working default so Jarvis boots with zero setup. Optional
capabilities (voice, Safe Browsing, YouTube API) switch on when their keys or
packages are present and stay quietly disabled otherwise.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent


def _home() -> Path:
    return Path(os.environ.get("JARVIS_HOME", Path.home() / ".jarvis")).expanduser()


def _resolve_data_dir() -> Path:
    """Find the editable data files.

    Checked in order so the same code works from a git checkout, an editable
    install, a wheel that bundled the files inside the package, and a user who
    keeps their own copy under JARVIS_HOME.
    """
    override = os.environ.get("JARVIS_DATA_DIR")
    candidates = [
        Path(override).expanduser() if override else None,
        PROJECT_ROOT / "data",
        PACKAGE_ROOT / "data",
        _home() / "data",
    ]
    for candidate in candidates:
        if candidate and (candidate / "universe.txt").exists():
            return candidate
    return PROJECT_ROOT / "data"


DATA_DIR = _resolve_data_dir()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    # --- identity -------------------------------------------------------
    owner_name: str = os.environ.get("JARVIS_OWNER", "Caleb")
    wake_phrase: str = os.environ.get("JARVIS_WAKE_PHRASE", "hey jarvis")

    # --- storage --------------------------------------------------------
    home: Path = field(default_factory=_home)

    # --- market data ----------------------------------------------------
    universe_file: Path = DATA_DIR / "universe.txt"
    curated_sources_file: Path = DATA_DIR / "curated_sources.yaml"
    seed_knowledge_file: Path = DATA_DIR / "seed_knowledge.yaml"
    news_feeds_file: Path = DATA_DIR / "news_feeds.yaml"

    # --- scanning -------------------------------------------------------
    scan_concurrency: int = _env_int("JARVIS_SCAN_CONCURRENCY", 24)
    scan_interval_seconds: int = _env_int("JARVIS_SCAN_INTERVAL", 60)
    scan_batch_size: int = _env_int("JARVIS_SCAN_BATCH", 100)
    min_universe_size: int = 500

    # --- learning -------------------------------------------------------
    learn_interval_seconds: int = _env_int("JARVIS_LEARN_INTERVAL", 1800)
    news_interval_seconds: int = _env_int("JARVIS_NEWS_INTERVAL", 300)
    # A lesson needs this many scored outcomes before Jarvis trusts it enough
    # to surface it as expertise rather than as a hypothesis.
    lesson_promotion_samples: int = _env_int("JARVIS_PROMOTION_SAMPLES", 20)
    lesson_promotion_confidence: float = 0.62

    # --- safety ---------------------------------------------------------
    safe_browsing_key: str | None = os.environ.get("GOOGLE_SAFE_BROWSING_KEY")
    require_safe_browsing: bool = _env_flag("JARVIS_REQUIRE_SAFE_BROWSING", False)
    http_timeout: int = _env_int("JARVIS_HTTP_TIMEOUT", 20)

    # --- integrations ---------------------------------------------------
    youtube_api_key: str | None = os.environ.get("YOUTUBE_API_KEY")
    google_cse_key: str | None = os.environ.get("GOOGLE_CSE_KEY")
    google_cse_id: str | None = os.environ.get("GOOGLE_CSE_ID")

    # --- voice ----------------------------------------------------------
    voice_enabled: bool = _env_flag("JARVIS_VOICE", False)
    porcupine_key: str | None = os.environ.get("PICOVOICE_ACCESS_KEY")

    def __post_init__(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.home / "jarvis.db"

    @property
    def cache_dir(self) -> Path:
        path = self.home / "cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def log_path(self) -> Path:
        return self.home / "jarvis.log"


_config: Config | None = None


def get_config() -> Config:
    """Process-wide config singleton."""
    global _config
    if _config is None:
        _config = Config()
    return _config


def reset_config() -> None:
    """Drop the cached config. Used by tests that repoint ``JARVIS_HOME``."""
    global _config
    _config = None
