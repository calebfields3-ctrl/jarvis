"""Jarvis -- a self-learning AI trading assistant."""

from .brain import Jarvis
from .config import Config, get_config

__version__ = "1.0.0"
__all__ = ["Jarvis", "Config", "get_config"]
