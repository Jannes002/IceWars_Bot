from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# ── TTL-Cache für selten ändernde API-Daten ───────────────────────────────────
# key → (data, expires_at_monotonic)
_cache: dict[str, tuple[Any, float]] = {}

_CACHE_TTL: dict[str, float] = {
    "research":  5 * 60,    # 5 Minuten (FP ändern sich laufend → can_afford muss aktuell sein)
    "highscore": 15 * 60,   # 15 Minuten pro Kategorie (4 Endpunkte)
}


def _cache_get(key: str) -> Optional[Any]:
    entry = _cache.get(key)
    if entry is None:
        return None
    data, expires = entry
    if _time.monotonic() > expires:
        del _cache[key]
        return None
    return data


def _cache_set(key: str, data: Any, ttl: float) -> None:
    _cache[key] = (data, _time.monotonic() + ttl)


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

# UI-Only-Writes Regel: Schreibende API-Aufrufe wurden entfernt.
# Alle mutierenden Aktionen (Bau, Forschung, Spende) laufen ausschließlich
# über die Weboberfläche in icewars_bot/actions.py (Playwright-Klicks).
# Hier bleiben nur noch GET-Leser.

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

    async def get_all_planets(self) -> list[dict]:
        """Liest alle Planeten aus dem Spiel.

        Strategie (mehrere Quellen, werden zusammengeführt):
        1. loadPlanetSelector() aufrufen → befüllt _allPlanets im Spiel
        2. _allPlanets JS-Variable auslesen
        3. /api/city/ → colonies Feld (Fallback, auch Integer-IDs)
        4. /api/colonies/ oder ähnliche Endpunkte probieren

        Gibt Liste von Dicts mit mindestens {id} zurück.
        """
        _LOAD_AND_READ_JS = """
        async (token) => {
            const result = {planets: [], sources: [], errors: []};

            // 1. loadPlanetSelector() aufrufen um _allPlanets zu befüllen
            try {
                if (typeof loadPlanetSelector === 'function') {
                    loadPlanetSelector();
                    result.sources.push('loadPlanetSelector_called');
                }
            } catch(e) { result.errors.push('loadPlanetSelector: ' + e.message); }

            // 2. _allPlanets auslesen
            try {
                if (typeof _allPlanets !== 'undefined' && Array.isArray(_allPlanets) && _allPlanets.length > 0) {
                    const sample = _allPlanets[0];
                    result.allPlanets_keys = Object.keys(sample);
                    result.allPlanets_sample = JSON.stringify(sample).slice(0, 300);
                    for (const p of _allPlanets) {
                        const id = p.id || p.city_id || p.cityId;
                        if (id) result.planets.push({
                            id, name: p.name || p.city_name || p.cityName || '',
                            coords: p.coords || p.coord || p.coordinates || '',
                            planet_type: p.planet_type || p.planetType || p.type || '',
                            source: '_allPlanets',
                        });
                    }
                    result.sources.push('_allPlanets:' + _allPlanets.length);
                } else {
                    result.allPlanets_status = typeof _allPlanets === 'undefined' ? 'undefined' : (Array.isArray(_allPlanets) ? 'empty_array' : typeof _allPlanets);
                }
            } catch(e) { result.errors.push('_allPlanets: ' + e.message); }

            // 3. /api/city/ colonies Feld — raw
            try {
                const headers = {'Authorization': 'Bearer ' + token};
                const r = await fetch('/api/city/', {headers});
                if (r.ok) {
                    const data = await r.json();
                    const cols = data.colonies || [];
                    result.colonies_raw_type = Array.isArray(cols) ? 'array[' + cols.length + ']' : typeof cols;
                    result.colonies_sample = JSON.stringify(cols).slice(0, 300);
                    const seenIds = new Set(result.planets.map(p => p.id));
                    for (const c of cols) {
                        let id = null;
                        if (typeof c === 'number') id = c;
                        else if (typeof c === 'object' && c) id = c.id || c.city_id;
                        if (id && !seenIds.has(id)) {
                            seenIds.add(id);
                            const entry = {id, source: 'api_colonies'};
                            if (typeof c === 'object' && c) {
                                entry.name = c.name || c.city_name || '';
                                entry.coords = c.coords || c.coord || '';
                                entry.planet_type = c.planet_type || c.type || '';
                            }
                            result.planets.push(entry);
                        }
                    }
                    result.sources.push('api_city_colonies');
                }
            } catch(e) { result.errors.push('api_city: ' + e.message); }

            // 4. /api/colonies/ probieren
            try {
                const headers = {'Authorization': 'Bearer ' + token};
                for (const ep of ['/api/colonies/', '/api/empire/', '/api/planets/']) {
                    const r = await fetch(ep, {headers});
                    if (r.ok) {
                        const data = await r.json();
                        result['extra_' + ep] = JSON.stringify(data).slice(0, 200);
                        result.sources.push(ep);
                        break;
                    }
                }
            } catch(e) { result.errors.push('extra_endpoints: ' + e.message); }

            return result;
        }
        """
        try:
            token = await self._page.evaluate("() => localStorage.getItem('icewars_token')")
            result = await self._page.evaluate(_LOAD_AND_READ_JS, token)

            if not isinstance(result, dict):
                logger.warning("get_all_planets: unerwartetes Ergebnis: %s", result)
                return []

            # Diagnose-Ausgabe
            logger.info(
                "get_all_planets: %d Planeten | Quellen: %s | _allPlanets: %s | colonies_raw: %s | Fehler: %s",
                len(result.get("planets", [])),
                result.get("sources", []),
                result.get("allPlanets_status", f"sample={result.get('allPlanets_sample', '')[:80]}"),
                result.get("colonies_raw_type", "?") + " " + result.get("colonies_sample", "")[:80],
                result.get("errors", []),
            )

            return result.get("planets", [])

        except Exception as e:
            logger.warning("get_all_planets fehlgeschlagen: %s", e)
            return []

    # HINWEIS: Schreibende API-Aufrufe wurden bewusst entfernt (UI-Only-Writes).
    # Allianz-Spenden laufen jetzt ausschließlich über
    # ``ActionExecutor.donate_to_alliance`` (actions.py), das die Web-UI bedient.

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

    async def dump_colony_diagnostics(self) -> None:
        """Einmalige Diagnose: liest alle Colony-relevanten DOM-Elemente + JS-Funktionen aus.

        Schreibt das Ergebnis nach logs/colony_diagnostics.json für manuelle Analyse.
        Wird beim ersten Start automatisch aufgerufen.
        """
        _DIAG_JS = """
        () => {
            const result = {
                window_fns: [],
                colony_elements: [],
                onclick_samples: [],
                global_keys_city: [],
            };

            // 1) Welche Funktionen gibt es auf window mit "city" im Namen?
            result.window_fns = Object.getOwnPropertyNames(window)
                .filter(k => typeof window[k] === 'function' &&
                    /city|kolonie|colony|planet|select|switch/i.test(k));

            // 2) Alle Elemente mit onclick die "city" enthalten
            const allEls = document.querySelectorAll('[onclick]');
            allEls.forEach(el => {
                const oc = el.getAttribute('onclick') || '';
                if (/city|kolonie|colony|planet/i.test(oc)) {
                    result.onclick_samples.push({
                        tag: el.tagName,
                        id: el.id || '',
                        class: el.className || '',
                        onclick: oc.substring(0, 200),
                        text: (el.textContent || '').trim().substring(0, 50),
                    });
                }
            });

            // 3) Elemente mit data-city* oder data-colony* oder data-planet* Attributen
            const dataEls = document.querySelectorAll('[data-city-id],[data-id],[data-colony-id],[data-planet-id]');
            dataEls.forEach(el => {
                result.colony_elements.push({
                    tag: el.tagName,
                    id: el.id || '',
                    class: el.className || '',
                    dataset: JSON.stringify(el.dataset),
                    onclick: (el.getAttribute('onclick') || '').substring(0, 200),
                    href: el.getAttribute('href') || '',
                    text: (el.textContent || '').trim().substring(0, 50),
                });
            });

            // 4) window-Keys die "city" enthalten (auch nicht-Funktionen)
            result.global_keys_city = Object.getOwnPropertyNames(window)
                .filter(k => /city|kolonie|colony|planet/i.test(k))
                .slice(0, 30);

            return result;
        }
        """
        try:
            import json
            import os
            os.makedirs("logs", exist_ok=True)
            data = await self._page.evaluate(_DIAG_JS)
            out_path = "logs/colony_diagnostics.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info("Colony-Diagnostik gespeichert: %s", out_path)
            logger.info("window-Funktionen mit city/colony/planet: %s", data.get("window_fns", []))
            logger.info("onclick-Samples: %d Elemente", len(data.get("onclick_samples", [])))
            logger.info("data-*-Elemente: %d Elemente", len(data.get("colony_elements", [])))
        except Exception as e:
            logger.warning("Colony-Diagnostik fehlgeschlagen: %s", e)

    async def switch_to_city(self, city_id: int) -> bool:
        """Wechselt im Spiel zur Kolonie mit der gegebenen ID.

        Nutzt die echten Spielfunktionen: changePlanet(), loadCityData(),
        setPlanetSelectValue() — ermittelt via dump_colony_diagnostics().
        Gibt True zurück wenn /api/city/ die neue city_id bestätigt.
        """
        async def _confirm_switched(wait: float = 2.0) -> bool:
            try:
                await asyncio.sleep(wait)
                check = await self._api_get("/api/city/")
                actual = int(check.get("id", -1))
                if actual == city_id:
                    return True
                logger.debug("Koloniewechsel-Check: API=%d erwartet=%d", actual, city_id)
                return False
            except Exception as e:
                logger.debug("Koloniewechsel-Check fehlgeschlagen: %s", e)
                return False

        # ── 1. changePlanet(city_id) — primäre Spielfunktion ─────────────
        try:
            await self._page.evaluate(f"changePlanet({city_id})")
            if await _confirm_switched():
                logger.info("Koloniewechsel zu %d via changePlanet()", city_id)
                return True
        except Exception as e:
            logger.debug("changePlanet() fehlgeschlagen: %s", e)

        # ── 2. loadCityData(city_id) — alternative Ladefunktion ──────────
        try:
            await self._page.evaluate(f"loadCityData({city_id})")
            if await _confirm_switched():
                logger.info("Koloniewechsel zu %d via loadCityData()", city_id)
                return True
        except Exception as e:
            logger.debug("loadCityData() fehlgeschlagen: %s", e)

        # ── 3. setPlanetSelectValue + Trigger ─────────────────────────────
        # Das Dropdown auf den Wert setzen und ein change-Event feuern
        try:
            await self._page.evaluate(f"""
                setPlanetSelectValue({city_id});
                const sel = document.querySelector('select[onchange*="changePlanet"], #planet-select, #city-select');
                if (sel) {{
                    sel.value = {city_id};
                    sel.dispatchEvent(new Event('change'));
                }}
            """)
            if await _confirm_switched():
                logger.info("Koloniewechsel zu %d via setPlanetSelectValue()", city_id)
                return True
        except Exception as e:
            logger.debug("setPlanetSelectValue() fehlgeschlagen: %s", e)

        # ── 4. Select-Element direkt per DOM setzen ───────────────────────
        try:
            switched = await self._page.evaluate(f"""
                (() => {{
                    const selectors = [
                        'select[onchange]',
                        '#planet-switcher',
                        '#colony-select',
                        '.planet-select',
                    ];
                    for (const s of selectors) {{
                        const el = document.querySelector(s);
                        if (el && el.tagName === 'SELECT') {{
                            const opt = Array.from(el.options).find(o => +o.value === {city_id});
                            if (opt) {{
                                el.value = {city_id};
                                el.dispatchEvent(new Event('change'));
                                return true;
                            }}
                        }}
                    }}
                    return false;
                }})()
            """)
            if switched and await _confirm_switched():
                logger.info("Koloniewechsel zu %d via Select-DOM", city_id)
                return True
        except Exception as e:
            logger.debug("Select-DOM fehlgeschlagen: %s", e)

        logger.warning("Koloniewechsel zu %d fehlgeschlagen — alle Methoden erfolglos.", city_id)
        return False

    async def scrape(self) -> dict[str, Any]:
        """Holt Stadtdaten, Forschungsliste und aktiven Forschungsstatus."""
        logger.debug("Scrape läuft...")

        city = await self._api_get("/api/city/")
        if "_error" in city:
            logger.error("API /city/ Fehler: HTTP %s", city.get("_error", "?"))
            return {}

        research_data = _cache_get("research")
        if research_data is None:
            research_data = await self._api_get("/api/research/")
            if "_error" not in research_data:
                _cache_set("research", research_data, _CACHE_TTL["research"])
                logger.debug("Research-Daten gecacht (TTL 30 min)")
            else:
                research_data = {}
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

        # Highscore-Daten holen (alle Kategorien) — gecacht für 15 Minuten
        highscore = {}
        for category in ("points", "research", "fleet", "economy"):
            cache_key = f"highscore_{category}"
            hs = _cache_get(cache_key)
            if hs is None:
                hs = await self._api_get(f"/api/highscore?category={category}")
                if "_error" not in hs:
                    _cache_set(cache_key, hs, _CACHE_TTL["highscore"])
                    logger.debug("Highscore '%s' gecacht (TTL 15 min)", category)
                else:
                    logger.debug("Highscore '%s' nicht verfügbar: %s", category, hs)
                    hs = {}
            if hs:
                highscore[category] = hs

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
