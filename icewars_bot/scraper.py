from __future__ import annotations

import asyncio
import json
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

# JavaScript: dumpt die Gebäude-Ansicht — alle baubaren Gebäude mit Kosten,
# Bauzeit, Level und ob der Button gerade aktiv ist. Wird für das Scoring
# (Kosten/Nutzen/Zeit) in strategy.py benötigt.
_BUILD_VIEW_DUMP_JS = r"""
() => {
    const view = document.getElementById('view-build');
    if (!view) return { error: 'no_view_build', html_length: 0, options: [] };

    function parseNumber(s) {
        if (!s) return 0;
        // "1.234" / "1,234" / "1234" → 1234
        const cleaned = String(s).replace(/[^\d.,-]/g, '').replace(/[.,](?=\d{3}\b)/g, '');
        const n = parseFloat(cleaned.replace(',', '.'));
        return isNaN(n) ? 0 : n;
    }

    function parseTime(text) {
        if (!text) return 0;
        // Formate: "1h 30m 45s", "30m 45s", "45s", "1:30:45", "01:30", "90 min"
        let sec = 0;
        const hms = text.match(/(\d+)\s*h\s*(\d+)\s*m(?:\s*(\d+)\s*s)?/i);
        if (hms) {
            sec = parseInt(hms[1])*3600 + parseInt(hms[2])*60 + parseInt(hms[3]||'0');
            return sec;
        }
        const ms = text.match(/(\d+)\s*m(?:in)?\s*(\d+)?\s*s?/i);
        if (ms) {
            sec = parseInt(ms[1])*60 + parseInt(ms[2]||'0');
            return sec;
        }
        const colon = text.match(/(\d+):(\d{2}):(\d{2})/);
        if (colon) {
            return parseInt(colon[1])*3600 + parseInt(colon[2])*60 + parseInt(colon[3]);
        }
        const colon2 = text.match(/(\d+):(\d{2})(?!:)/);
        if (colon2) {
            return parseInt(colon2[1])*60 + parseInt(colon2[2]);
        }
        const onlySec = text.match(/(\d+)\s*s\b/i);
        if (onlySec) return parseInt(onlySec[1]);
        const onlyMin = text.match(/(\d+)\s*min/i);
        if (onlyMin) return parseInt(onlyMin[1])*60;
        return 0;
    }

    // Kosten-Keywords → Zielschlüssel (alle Varianten abdecken)
    const costKeys = [
        ['iron',      /\bEisen\b/i],
        ['steel',     /\bStahl\b/i],
        ['chemicals', /\bChem/i],
        ['ice',       /\bEis\b/i],
        ['water',     /\bWasser\b/i],
        ['energy',    /\bEnergie\b/i],
        ['vv4a',      /\bVV4A\b/i],
        ['credits',   /\bCredits?\b/i],
        ['population',/\bBev[öo]lk|\bSiedler\b|\bEinwohner\b/i],
    ];

    const options = [];

    // Alle Buttons mit startBuild finden
    const buttons = view.querySelectorAll("button[onclick*='startBuild']");
    buttons.forEach(btn => {
        const onclick = btn.getAttribute('onclick') || '';
        const m = onclick.match(/startBuild\(['"]([^'"]+)['"]\)/);
        if (!m) return;
        const btype = m[1];

        // Container-Zeile finden
        const row = btn.closest('tr') || btn.closest('.build-item') || btn.closest('li') || btn.parentElement;
        if (!row) return;

        const rowText = (row.innerText || row.textContent || '').replace(/\s+/g, ' ').trim();

        // Name: erstes nicht-leeres strong/td/div/span
        let name = '';
        const nameEl = row.querySelector('.building-name, .bname, strong, b, td:first-child, .name');
        if (nameEl) name = (nameEl.innerText || nameEl.textContent || '').trim();
        if (!name) {
            // Fallback: ersten Text-Chunk vor dem Doppelpunkt
            const firstChunk = rowText.split(/[:|·]/)[0].trim();
            name = firstChunk.split(/\s+\d/)[0].trim() || btype;
        }

        // Kosten: innerHTML nach Zahlen mit nachfolgendem Ressourcen-Label durchsuchen
        // Strategie: innerText in Tokens zerlegen, jede Zahl mit dem nächsten Label verbinden
        const cost = {};
        const tokens = rowText.split(/\s+/);
        for (let i = 0; i < tokens.length; i++) {
            const t = tokens[i];
            // Zahl (ggf. mit Tausenderpunkt)?
            if (!/^[\d.,]+$/.test(t)) continue;
            const val = parseNumber(t);
            if (val <= 0) continue;
            // Nächstes Wort = Einheit/Ressource
            const next = (tokens[i+1] || '').replace(/[^A-Za-zäöüÄÖÜ]/g, '');
            for (const [key, re] of costKeys) {
                if (re.test(next)) {
                    cost[key] = val;
                    break;
                }
            }
        }

        // Fallback: globales Regex über rowText
        if (Object.keys(cost).length === 0) {
            for (const [key, re] of costKeys) {
                const match = rowText.match(new RegExp('([\\d.,]+)\\s*(?:' + re.source.slice(2, -2) + ')', 'i'));
                if (match) cost[key] = parseNumber(match[1]);
            }
        }

        // Bauzeit: Suche nach Zeit-Pattern im Zeilentext
        let time_sec = 0;
        const timeRe = /(\d+h\s*\d+m\s*\d+s|\d+h\s*\d+m|\d+m\s*\d+s|\d+:\d{2}:\d{2}|\d+:\d{2}|\d+\s*min|\d+\s*s\b)/i;
        const tm = rowText.match(timeRe);
        if (tm) time_sec = parseTime(tm[1]);

        // Level / Anzahl aus "Stufe X" oder "Lvl. X" oder "(X)"
        let level = 0;
        const lvl = rowText.match(/Stufe\s*(\d+)|Lvl\.?\s*(\d+)|\((\d+)\)/i);
        if (lvl) level = parseInt(lvl[1] || lvl[2] || lvl[3] || '0');

        const disabled = btn.disabled || btn.getAttribute('disabled') !== null ||
                         (btn.className || '').includes('disabled');

        options.push({
            building_type: btype,
            name: name.slice(0, 80),
            cost,
            time_sec,
            level,
            is_buildable: !disabled,
            row_text: rowText.slice(0, 300),   // Rohtext zur Diagnose
            button_classes: btn.className || '',
            button_disabled: disabled,
        });
    });

    return {
        error: null,
        view_visible: view.style.display !== 'none',
        options_count: options.length,
        options,
        raw_view_text: (view.innerText || '').slice(0, 2000),
    };
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

    async def dump_build_view(self, out_dir: str | Path = "logs") -> Path:
        """Diagnose-Dump der Gebäude-Ansicht.

        Öffnet die Gebäude-Ansicht (#btn-gebbau), versucht alle relevanten
        API-Endpunkte zu finden und extrahiert das DOM mit Kosten, Bauzeit,
        Level und Status jedes Gebäudes. Schreibt:

        - logs/build_dump.json  — strukturierter Dump (API + DOM)
        - logs/build_view.html  — vollständiger HTML-Snapshot zur Analyse

        Gibt den Pfad zur JSON-Datei zurück.
        """
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        logger.info("Build-View-Dump startet...")

        # 1) Gebäudeansicht öffnen
        try:
            await self._page.click("#btn-gebbau")
            await asyncio.sleep(1.5)
        except Exception as e:
            logger.warning("Konnte #btn-gebbau nicht klicken: %s", type(e).__name__)

        # Versteckte Gebäude einblenden, falls Checkbox da ist
        try:
            cb = await self._page.query_selector("#show-hidden-buildings")
            if cb and not await cb.is_checked():
                await cb.click()
                await asyncio.sleep(0.5)
        except Exception:
            pass

        dump: dict[str, Any] = {
            "timestamp": "",
            "url": self._page.url,
        }

        # 2) Zeitstempel
        from datetime import datetime, timezone
        dump["timestamp"] = datetime.now(timezone.utc).isoformat()

        # 3) API-Endpunkte durchprobieren
        api_candidates = [
            "/api/city/",
            "/api/buildings/",
            "/api/build/",
            "/api/city/buildings",
        ]
        dump["api"] = {}
        for ep in api_candidates:
            try:
                result = await self._api_get(ep)
                dump["api"][ep] = result
            except Exception as e:
                dump["api"][ep] = {"_exception": type(e).__name__}

        # 4) DOM-Extraktion: Gebäude-Optionen mit Kosten/Zeit/Level
        try:
            dom_data = await self._page.evaluate(_BUILD_VIEW_DUMP_JS)
            dump["dom"] = dom_data
        except Exception as e:
            logger.error("DOM-Extraktion fehlgeschlagen: %s", e)
            dump["dom"] = {"_exception": str(e)}

        # 5) HTML-Snapshot der kompletten Build-View
        html_path = out_path / "build_view.html"
        try:
            html = await self._page.content()
            html_path.write_text(html, encoding="utf-8")
            dump["html_snapshot"] = str(html_path)
        except Exception as e:
            dump["html_snapshot_error"] = str(e)

        # 6) Ausschnitt des #view-build-Containers als separate Datei
        try:
            view_html = await self._page.evaluate(
                "() => { const v = document.getElementById('view-build'); return v ? v.outerHTML : ''; }"
            )
            if view_html:
                (out_path / "view_build.html").write_text(view_html, encoding="utf-8")
                dump["view_build_html"] = str(out_path / "view_build.html")
        except Exception:
            pass

        # 7) JSON schreiben
        json_path = out_path / "build_dump.json"
        json_path.write_text(
            json.dumps(dump, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        # 8) Kurzer Überblick ins Log
        dom = dump.get("dom", {})
        opt_count = dom.get("options_count", 0) if isinstance(dom, dict) else 0
        logger.info(
            "Build-View-Dump fertig: %d Gebäude-Optionen erkannt → %s",
            opt_count, json_path.resolve(),
        )
        if opt_count > 0:
            sample = dom.get("options", [])[:3]
            for o in sample:
                logger.info(
                    "  Beispiel: type=%s name=%r cost=%s time=%ds buildable=%s",
                    o.get("building_type"), o.get("name"),
                    o.get("cost"), o.get("time_sec"), o.get("is_buildable"),
                )

        return json_path
