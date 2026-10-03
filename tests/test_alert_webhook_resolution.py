"""Regression tests for SBOX alert-webhook resolution + .env loading.

Covers the 2026-10-03 E3 fix: the repo ``.env`` sets ``WEBHOOK_SBOX`` but the
dispatcher's canonical name is ``DISCORD_WEBHOOK_SBOX``; and cron-launched
processes have no shell init that sources ``.env``. Both are now handled.
"""

import importlib
import os

import pytest

from core.env_file import load_env_file, parse_env_text
from core import alert_dispatcher


def test_parse_env_ignores_comments_blanks_and_malformed():
    text = (
        "# comment\n"
        "\n"
        "NWP_ANALOG_ENABLED=1\n"
        "KALSHI_KEY_ID=abc123\n"
        "-l e\n"            # stray token (the real .env has a bare '-e')
        "no_equals_line\n"  # malformed
        "WEBHOOK_SBOX=https://discord/x/y\n"
    )
    d = parse_env_text(text)
    assert d["NWP_ANALOG_ENABLED"] == "1"
    assert d["KALSHI_KEY_ID"] == "abc123"
    assert d["WEBHOOK_SBOX"] == "https://discord/x/y"
    # malformed / stray lines are skipped, never executed
    assert "no_equals_line" not in d
    assert "-l e" not in d


def test_parse_env_strips_matching_quotes():
    d = parse_env_text('A="quoted"\nB=\'single\'\n')
    assert d["A"] == "quoted"
    assert d["B"] == "single"


def test_load_env_file_no_clobber(tmp_path, monkeypatch):
    f = tmp_path / "e.env"
    f.write_text("FOO=fromfile\nBAR=fromfile\n")
    monkeypatch.setenv("FOO", "fromenv")
    monkeypatch.delenv("BAR", raising=False)
    n = load_env_file(str(f))
    assert n == 1
    assert os.environ["FOO"] == "fromenv"   # real env wins
    assert os.environ["BAR"] == "fromfile"


def _reload_dispatcher():
    return importlib.reload(alert_dispatcher)


def test_sbox_resolves_short_webhook_name(monkeypatch):
    """Only WEBHOOK_SBOX set (as in the repo .env) -> SBOX URL resolves."""
    monkeypatch.delenv("DISCORD_WEBHOOK_SBOX", raising=False)
    monkeypatch.setenv("WEBHOOK_SBOX", "https://discord.example/sbox")
    mod = _reload_dispatcher()
    url = mod._get_webhook_url("SBOX")
    assert url == "https://discord.example/sbox"


def test_sbox_prefers_canonical_name(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_SBOX", "https://discord.example/canon")
    monkeypatch.setenv("WEBHOOK_SBOX", "https://discord.example/short")
    mod = _reload_dispatcher()
    assert mod._get_webhook_url("SBOX") == "https://discord.example/canon"


def test_sbox_missing_returns_none(monkeypatch):
    # Disable the import-time .env auto-load so the "missing" case is real.
    import core.env_file as env_file
    monkeypatch.setattr(env_file, "load_env_file", lambda *a, **k: 0)
    for k in ("DISCORD_WEBHOOK_SBOX", "WEBHOOK_SBOX"):
        monkeypatch.delenv(k, raising=False)
    mod = _reload_dispatcher()
    assert mod._get_webhook_url("SBOX") is None
