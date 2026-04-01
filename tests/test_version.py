"""Tests for version.py — hatch-vcs _version.py and dev fallback."""

import types
from unittest.mock import patch

from timba.version import get_version


def _fake_version(ver: str):
    """Fake a _version module with __version__ set."""
    mod = types.ModuleType("timba._version")
    mod.__version__ = ver
    return patch.dict("sys.modules", {"timba._version": mod})


def _no_version():
    """Make _version import fail."""
    return patch.dict("sys.modules", {"timba._version": None})


def test_version_from_hatch_vcs():
    """When _version.py exists (normal install), use it."""
    with _fake_version("1.2.3"):
        assert get_version() == "1.2.3"


def test_version_with_tag():
    """Full git tag version."""
    with _fake_version("v0.1.0"):
        assert get_version() == "v0.1.0"


def test_version_dev_build():
    """Dev version from hatch-vcs without tags."""
    with _fake_version("0.1.dev0+d20260401"):
        assert get_version() == "0.1.dev0+d20260401"


def test_version_fallback_dev():
    """When _version.py doesn't exist, return 'dev'."""
    with _no_version():
        assert get_version() == "dev"


def test_version_returns_string():
    v = get_version()
    assert isinstance(v, str)
    assert len(v) > 0
