"""Configuration error types.

Split into their own module so every other module in the package can
import them without forming a cycle back through the models.
"""

from __future__ import annotations


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or is invalid."""


class ConfigNotFoundError(ConfigError):
    """Raised when .jailbee/config.yaml is missing in the current directory."""
