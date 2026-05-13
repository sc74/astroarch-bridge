"""Test config: token auto-gen, env override, validazione."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from astroarch_bridge import config


def test_default_token_autogen(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROARCH_TOKEN_FILE", str(tmp_path / "tok"))
    monkeypatch.setenv("ASTROARCH_AUTO_GENERATE_TOKEN", "true")
    monkeypatch.delenv("ASTROARCH_TOKEN", raising=False)
    config.reset_settings_cache()
    s = config.get_settings()
    tok = s.resolve_token()
    assert tok and len(tok) >= 32
    # Stesso token alla seconda chiamata
    assert s.resolve_token() == tok
    assert (tmp_path / "tok").read_text().strip() == tok


def test_env_token_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROARCH_TOKEN", "fixed-token-123")
    monkeypatch.setenv("ASTROARCH_TOKEN_FILE", str(tmp_path / "tok"))
    config.reset_settings_cache()
    s = config.get_settings()
    assert s.resolve_token() == "fixed-token-123"


def test_log_level_validation(monkeypatch):
    monkeypatch.setenv("ASTROARCH_LOG_LEVEL", "BOGUS")
    config.reset_settings_cache()
    with pytest.raises(Exception):
        config.get_settings()


def test_port_range(monkeypatch):
    monkeypatch.setenv("ASTROARCH_PORT", "99999")
    config.reset_settings_cache()
    with pytest.raises(Exception):
        config.get_settings()
