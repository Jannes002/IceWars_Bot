"""Tests für den Goals-Store — insbesondere die neuen pausierbaren
Ressourcen. Die autouse-Fixture in conftest.py leitet Persistenz auf
tmp_path um; die echte data/goals.json bleibt unberührt.
"""
from __future__ import annotations

from icewars_bot import goals as G


def test_defaults_have_empty_paused_list():
    assert G.get()["paused_resources"] == []


def test_is_resource_paused_default_false():
    assert not G.is_resource_paused("ice")
    assert not G.is_resource_paused("water")


def test_update_paused_resources_roundtrip(tmp_path):
    G.update({"paused_resources": ["ice", "water"]})
    assert sorted(G.paused_resources()) == ["ice", "water"]
    assert G.is_resource_paused("ice")
    assert G.is_resource_paused("water")
    assert not G.is_resource_paused("iron")


def test_update_replaces_list_not_merges():
    """Listen werden komplett ersetzt (kein Merge) — UI-Toggles müssen
    Einträge zuverlässig entfernen können."""
    G.update({"paused_resources": ["ice", "water"]})
    G.update({"paused_resources": ["ice"]})  # water entfernt
    assert G.paused_resources() == ["ice"]
    assert not G.is_resource_paused("water")


def test_paused_resources_persists_across_reload():
    """Update → löscht In-Memory-State → get() lädt frisch von Disk →
    Pause muss erhalten bleiben."""
    G.update({"paused_resources": ["ice"]})
    G._goals.clear()  # Simuliere Neustart
    assert G.paused_resources() == ["ice"]
    assert G.is_resource_paused("ice")


def test_is_resource_paused_handles_empty_and_unknown():
    assert not G.is_resource_paused("")
    assert not G.is_resource_paused("unbekannt_xyz")


def test_paused_resources_defensive_on_bad_type(tmp_path):
    """Wenn das JSON durch externe Hand auf einen Nicht-Listen-Typ gesetzt
    wurde, soll paused_resources() robust eine leere Liste liefern."""
    G.update({"paused_resources": ["ice"]})
    # Verbiege internen State auf einen falschen Typ
    G._goals["paused_resources"] = "ice"  # type: ignore[assignment]
    assert G.paused_resources() == []
    assert not G.is_resource_paused("ice")


def test_reset_clears_paused_list():
    G.update({"paused_resources": ["ice", "water"]})
    G.reset()
    assert G.paused_resources() == []
