from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_API_JS = """
async (endpoint) => {
    const token = localStorage.getItem('icewars_token');
    const resp = await fetch(endpoint, {
        headers: { 'Authorization': 'Bearer ' + token }
    });
    if (!resp.ok) return { _error: resp.status, _url: endpoint };
    return await resp.json();
}
"""

# JavaScript: liest die Bauwarteschlange direkt aus dem DOM
# Strategie: alle [data-rem]-Timer auf der Seite suchen, dann Kontext nach oben traversieren
# um den Gebäudenamen und das Fertigstellungsdatum zu finden.
_BUILD_QUEUE_JS = """
() => {
    function parseGermanDate(text) {
        // "bis 08.04.2026, 09:12:19" oder "08.04.2026, 09:12:19"
        const m = text.match(/(\\d{2})\\.(\\d{2})\\.(\\d{4}),?\\s+(\\d{2}):(\\d{2}):(\\d{2})/);
        if (!m) return '';
        // Umrechnen in ISO: YYYY-MM-DDTHH:MM:SS
        return `${m[3]}-${m[2]}-${m[1]}T${m[4]}:${m[5]}:${m[6]}`;
    }

    function extractName(container) {
        // Text des Containers durchsuchen – Zeilen mit "bis", Zahlen oder X überspringen
        const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null);
        let node;
        while ((node = walker.nextNode())) {
            const t = node.textContent.trim();
            if (!t || t.length < 2) continue;
            if (/^(\\d|bis |x$)/i.test(t)) continue;  // Datum, Zahl, "x"
            if (/^\\d+h\\s*\\d+m/.test(t)) continue;   // Countdown "1h 45m 8s"
            if (/^\\d{2}\\.\\d{2}\\./.test(t)) continue; // Datum
            return t;
        }
        return '';
    }

    const results = [];

    // Alle Countdown-Timer auf der Seite finden
    const allTimers = document.querySelectorAll('[data-rem]');
    allTimers.forEach(timer => {
        const rem = parseInt(timer.dataset.rem || '0', 10);
        if (rem <= 0) return;  // abgelaufene/leere Timer überspringen

        // Container-Zeile finden (tr, li, oder div-Ebene 1–5 aufwärts)
        let container = timer.parentElement;
        for (let i = 0; i < 6 && container; i++) {
            const tag = container.tagName.toLowerCase();
            if (tag === 'tr' || tag === 'li') break;
            // Breite div/td die wahrscheinlich eine Zeile sind
            if ((tag === 'div' || tag === 'td') && container.querySelectorAll('[data-rem]').length === 1) break;
            container = container.parentElement;
        }
        if (!container) container = timer.parentElement;

        // Gebäudename aus dem Container extrahieren
        const name = extractName(container);

        // Fertigstellungsdatum aus "bis DD.MM.YYYY, HH:MM:SS"-Text
        let finish_time = '';
        const fullText = container.innerText || container.textContent || '';
        finish_time = parseGermanDate(fullText);

        results.push({ name, remaining_sec: rem, finish_time });
    });

    return results;
}
"""

# JavaScript: prüft ob das Forschungslabor gerade belegt ist und liefert ggf. Infos
_RESEARCH_ACTIVE_JS = """
() => {
    // Tabelle mit aktiver Forschung sichtbar?
    const table = document.getElementById('research-queue-table');
    const empty = document.getElementById('research-active-empty');
    if (!table || !empty) return null;

    const isActive = (table.style.display !== 'none' && empty.style.display === 'none');
    if (!isActive) return null;

    // Daten aus der aktiven Forschungszeile lesen
    const timer = document.getElementById('research-countdown-timer');
    const nameEl = table.querySelector('tbody tr td div[style*="font-weight:bold"]');
    const imgEl  = table.querySelector('tbody tr td img');

    return {
        name:          nameEl ? nameEl.innerText.trim() : '',
        remaining_sec: timer  ? parseInt(timer.dataset.rem || '0', 10) : 0,
        total_sec:     timer  ? parseInt(timer.dataset.total || '0', 10) : 0,
    };
}
"""


class GameScraper:
    """Ruft den Spielzustand über die Icewars REST-API ab."""

    def __init__(self, page: Page) -> None:
        self._page = page

    async def _api_get(self, endpoint: str) -> dict[str, Any]:
        return await self._page.evaluate(_API_JS, endpoint)

    @staticmethod
    def _finish_time_from_rem(remaining_sec: int) -> str:
        """Berechnet ISO-8601 Fertigstellungszeit aus verbleibenden Sekunden."""
        from datetime import datetime, timezone, timedelta
        if remaining_sec <= 0:
            return ""
        finish = datetime.now(timezone.utc) + timedelta(seconds=remaining_sec)
        return finish.isoformat()

    @staticmethod
    def _clean_building_name(name: str) -> str:
        """Entfernt Suffixe wie ' (Bau: +1)' aus dem Gebäudenamen."""
        import re
        return re.sub(r'\s*\(Bau:.*?\)', '', name).strip()

    async def scrape(self) -> dict[str, Any]:
        """Holt Stadtdaten, Forschungsliste und aktiven Forschungsstatus."""
        logger.debug("Scrape läuft...")

        city = await self._api_get("/api/city/")
        if "_error" in city:
            logger.error("API /city/ Fehler: HTTP %s", city.get("_error", "?"))
            return {}

        research_data = await self._api_get("/api/research/")
        research = research_data.get("research", []) if isinstance(research_data, dict) else []

        # Bauwarteschlange aus dem DOM anreichern (Namen + Restzeit)
        build_queue_dom = await self._page.evaluate(_BUILD_QUEUE_JS)

        # Bauwarteschlange: DOM-Einträge (mit rem > 0) sind die Haupt-Quelle,
        # API-Daten (type) werden als Ergänzung genutzt.
        api_build_queue = city.get("build_queue", [])
        dom_entries = [d for d in build_queue_dom if d.get("remaining_sec", 0) > 0 or d.get("name")]

        merged_build_queue = []
        # Über DOM-Einträge iterieren (Reihenfolge wie im Spiel sichtbar)
        for i, dom in enumerate(dom_entries):
            api = api_build_queue[i] if i < len(api_build_queue) else {}
            # Name: DOM bevorzugen (enthält echten Anzeigenamen wie "Zelt"), API-Typ als Fallback
            raw_name = dom.get("name", "") or api.get("name", "") or api.get("type", "")
            name = self._clean_building_name(raw_name)
            # finish_time: DOM-geparste Datum bevorzugen, sonst aus rem berechnen, sonst API
            finish_time = (
                dom.get("finish_time", "")
                or api.get("finish_time", "")
                or self._finish_time_from_rem(dom.get("remaining_sec", 0))
            )
            remaining_sec = dom.get("remaining_sec", 0)
            btype = api.get("type", "")
            merged_build_queue.append({
                "type":          btype,
                "name":          name,
                "finish_time":   finish_time,
                "remaining_sec": remaining_sec,
            })
            if name or remaining_sec:
                logger.debug("BauQueue[%d]: '%s' type=%s rem=%ds finish=%s",
                             i, name, btype, remaining_sec, finish_time[:16] if finish_time else "—")

        # Fallback: wenn DOM leer, API-Daten direkt verwenden
        if not dom_entries and api_build_queue:
            for b in api_build_queue:
                ft = b.get("finish_time", "")
                merged_build_queue.append({
                    "type":          b.get("type", ""),
                    "name":          b.get("name", "") or b.get("type", ""),
                    "finish_time":   ft,
                    "remaining_sec": 0,
                })

        city["build_queue"] = merged_build_queue

        # Aktive Forschung aus dem DOM ermitteln (zuverlässiger als API-Polling)
        active_research_dom = await self._page.evaluate(_RESEARCH_ACTIVE_JS)

        # Aktive Forschung mit dem Forschungstyp aus der Liste abgleichen
        active_research: dict | None = None
        research_lab_busy = False

        if active_research_dom:
            research_lab_busy = True
            active_name = active_research_dom["name"].lower()
            # Typ per Namensabgleich finden
            matched_type = next(
                (r["type"] for r in research if r.get("name", "").lower() == active_name),
                "unknown",
            )
            rem = active_research_dom["remaining_sec"]
            active_research = {
                "type": matched_type,
                "name": active_research_dom["name"],
                "remaining_sec": rem,
                "finish_time": self._finish_time_from_rem(rem),
            }
            logger.info(
                "Labor belegt: '%s' (Typ=%s, noch %ds)",
                active_research_dom["name"],
                matched_type,
                rem,
            )
        else:
            logger.debug("Labor frei — keine aktive Forschung.")

        # Highscore-Daten holen (alle Kategorien)
        highscore = {}
        for category in ("points", "research", "fleet", "economy"):
            hs = await self._api_get(f"/api/highscore?category={category}")
            if "_error" not in hs:
                highscore[category] = hs
            else:
                logger.debug("Highscore '%s' nicht verfügbar: %s", category, hs)

        raw = {
            "city": city,
            "research": research,
            "active_research": active_research,
            "research_lab_busy": research_lab_busy,
            "highscore": highscore,
        }

        logger.info(
            "Scraped: %s | Eisen=%.0f Stahl=%.0f Eis=%.0f FP=%.0f Labor=%s Punkte=%d",
            city.get("coords", "?"),
            city.get("resources", {}).get("iron", 0),
            city.get("resources", {}).get("steel", 0),
            city.get("resources", {}).get("ice", 0),
            city.get("fp", 0),
            "belegt" if research_lab_busy else "frei",
            city.get("points", 0),
        )
        return raw

    async def snapshot(self, path: str | Path) -> None:
        """Speichert die aktuelle Seite als HTML für Offline-Analyse."""
        content = await self._page.content()
        Path(path).write_text(content, encoding="utf-8")
        logger.info("Snapshot gespeichert: %s", path)
