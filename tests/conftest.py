"""Shared pytest fixtures.

Wichtigste Aufgabe: den ``goals``-Store pro Test frisch machen, damit
Pausen/Prioritäten aus einem Test nicht in den nächsten lecken, und
vor allem NIE die echte ``data/goals.json`` verändern.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from icewars_bot import goals as G


@pytest.fixture(autouse=True)
def _reset_goals(tmp_path: Path, monkeypatch):
    """Leitet Goals-Persistenz auf ``tmp_path`` um und leert den In-Memory-State.

    - ``GOALS_PATH`` zeigt während des Tests auf einen Pfad in ``tmp_path``
      → kein Save wird die echte ``data/goals.json`` überschreiben.
    - ``_goals`` wird geleert, damit das Modul beim ersten ``get()`` frisch
      aus den Defaults lädt (Datei existiert nicht).
    """
    monkeypatch.setattr(G, "GOALS_PATH", tmp_path / "goals.json")
    G._goals.clear()
    yield
    G._goals.clear()
