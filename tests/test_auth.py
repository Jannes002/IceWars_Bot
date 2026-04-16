"""Tests für Auth-Helpers."""
from __future__ import annotations

from icewars_bot.auth import _normalize_game_url


def test_already_normalized_https_is_unchanged():
    assert _normalize_game_url("https://example.com/") == "https://example.com/"


def test_already_normalized_http_is_unchanged():
    assert _normalize_game_url("http://example.com") == "http://example.com"


def test_schemaless_host_gets_https_prefix():
    assert _normalize_game_url("www.mmo-icewars.de") == "https://www.mmo-icewars.de"


def test_schemaless_bare_host_gets_https_prefix():
    assert _normalize_game_url("mmo-icewars.de") == "https://mmo-icewars.de"


def test_protocol_relative_gets_https():
    assert _normalize_game_url("//example.com/path") == "https://example.com/path"


def test_whitespace_trimmed():
    assert _normalize_game_url("   www.example.com  ") == "https://www.example.com"


def test_empty_stays_empty():
    assert _normalize_game_url("") == ""
    assert _normalize_game_url("   ") == ""


def test_case_insensitive_protocol_detection():
    # https://X.Y soll unverändert bleiben, egal welche Großschreibung
    assert _normalize_game_url("HTTPS://example.com") == "HTTPS://example.com"
