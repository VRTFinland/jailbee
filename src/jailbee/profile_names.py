"""Shared validation for LiteLLM profile names."""

import re

PROFILE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
"""Profile names are embedded in proxy model aliases and must not contain dots."""
