"""Tests für die Telegram-Nachrichten aus BotLoop — insbesondere die
neue Fertigstellungs-Meldung mit Name, Stufe und Kolonie-Bezug.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from icewars_bot.bot import BotLoop
from icewars_bot.state import (
    BuildingInfo,
    BuildQueueItem,
    GameState,
)


def _make_bot(username: str = "Thuban9") -> BotLoop:
    """Erzeugt einen BotLoop ohne den vollen Konstruktor, damit wir nur
    die Attribute setzen, die für _check_completed_buildings / _colony_label
    gebraucht werden."""
    bot = BotLoop.__new__(BotLoop)
    bot._config = SimpleNamespace(auth=SimpleNamespace(username=username))
    bot._last_queue = []
    bot._tg = None  # _notify wird zum No-Op, wenn kein Notifier gesetzt ist

    # Abgeschickte Nachrichten aufzeichnen, indem wir _notify monkey-patchen.
    sent: list[str] = []

    async def capture(text: str) -> None:
        sent.append(text)

    bot._notify = capture  # type: ignore[attr-defined]
    bot._sent = sent       # type: ignore[attr-defined]
    return bot


# ── _colony_label ────────────────────────────────────────────────────────────


def test_colony_label_uses_city_name_when_present():
    bot = _make_bot()
    state = GameState(city_name="admin12345s Kolonie", coords="1:19:3")
    assert bot._colony_label(state) == "admin12345s Kolonie (1:19:3)"


def test_colony_label_falls_back_to_username():
    bot = _make_bot(username="Thuban9")
    state = GameState(city_name="", coords="")
    assert bot._colony_label(state) == "Thuban9s Kolonie"


def test_colony_label_without_coords():
    bot = _make_bot()
    state = GameState(city_name="Eis-Basis", coords="")
    assert bot._colony_label(state) == "Eis-Basis"


# ── _check_completed_buildings ───────────────────────────────────────────────


def test_first_run_no_notification():
    """Erste Runde hat keine Vergleichsbasis — darf nicht senden."""
    bot = _make_bot()
    state = GameState(build_queue=[BuildQueueItem("x", "X", finish_time="t1")])
    asyncio.run(bot._check_completed_buildings(state))
    assert bot._sent == []  # type: ignore[attr-defined]


def test_completed_building_message_has_name_level_colony():
    """Reproduziert den User-Wunsch:
    'Chemielager (Stufe 17) auf admin12345s Kolonie ist fertig'.
    """
    bot = _make_bot()
    # Vorige Runde: Chemielager war in der Queue bis t=xyz.
    bot._last_queue = [
        BuildQueueItem("chemicals_storage", "Chemielager", finish_time="2026-04-16T12:00:00")
    ]
    # Aktuelle Runde: Queue ist leer, BuildingInfo zeigt level=17.
    state = GameState(
        city_name="admin12345s Kolonie",
        coords="1:19:3",
        build_queue=[],
        buildings=[
            BuildingInfo(
                type="chemicals_storage", name="Chemielager",
                category="storage", count=17, level=17,
            )
        ],
    )
    asyncio.run(bot._check_completed_buildings(state))
    assert len(bot._sent) == 1  # type: ignore[attr-defined]
    msg = bot._sent[0]  # type: ignore[attr-defined]
    assert "Chemielager" in msg
    assert "Stufe 17" in msg
    assert "admin12345s Kolonie" in msg
    assert "fertig" in msg


def test_completed_without_building_info_still_sends():
    """Wenn das Gebäude aus buildings verschwunden ist (ungewöhnlich),
    senden wir wenigstens Name + Kolonie, ohne Stufen-Teil."""
    bot = _make_bot()
    bot._last_queue = [
        BuildQueueItem("house_small", "Kleines Wohnhaus", finish_time="t1")
    ]
    state = GameState(city_name="Basis-A", build_queue=[], buildings=[])
    asyncio.run(bot._check_completed_buildings(state))
    assert len(bot._sent) == 1  # type: ignore[attr-defined]
    msg = bot._sent[0]  # type: ignore[attr-defined]
    assert "Kleines Wohnhaus" in msg
    assert "Basis-A" in msg
    assert "Stufe" not in msg  # kein Level gefunden → weglassen


def test_still_in_queue_no_notification():
    """Wenn dieselbe finish_time auch in der aktuellen Queue steht,
    ist das Gebäude noch nicht fertig."""
    bot = _make_bot()
    item = BuildQueueItem("x", "X", finish_time="t1")
    bot._last_queue = [item]
    state = GameState(build_queue=[item])
    asyncio.run(bot._check_completed_buildings(state))
    assert bot._sent == []  # type: ignore[attr-defined]


def test_multiple_completions_each_sent():
    bot = _make_bot()
    bot._last_queue = [
        BuildQueueItem("a", "AAA", finish_time="t1"),
        BuildQueueItem("b", "BBB", finish_time="t2"),
    ]
    state = GameState(
        city_name="K1",
        build_queue=[],
        buildings=[
            BuildingInfo(type="a", name="AAA", category="x", level=3),
            BuildingInfo(type="b", name="BBB", category="x", level=5),
        ],
    )
    asyncio.run(bot._check_completed_buildings(state))
    assert len(bot._sent) == 2  # type: ignore[attr-defined]
    joined = "\n".join(bot._sent)  # type: ignore[attr-defined]
    assert "AAA" in joined and "Stufe 3" in joined
    assert "BBB" in joined and "Stufe 5" in joined
