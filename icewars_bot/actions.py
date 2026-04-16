from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Page

from .strategy import Action

logger = logging.getLogger(__name__)


class ActionExecutor:
    """Führt Spielaktionen über den Browser aus."""

    def __init__(self, page: Page) -> None:
        self._page = page

    async def _find_build_button(self, btype: str):
        """Findet einen Bau-Button anhand des Building-Types im onclick-Attribut."""
        target_onclick = f"startBuild('{btype}')"
        btn = await self._page.query_selector(f'button[onclick="{target_onclick}"]')
        if btn:
            return btn

        # Fallback: alle Buttons durchsuchen
        all_btns = await self._page.query_selector_all("#view-build button")
        for b in all_btns:
            onclick_val = await b.get_attribute("onclick") or ""
            if btype in onclick_val:
                return b
        return None

    async def execute(self, action: Action) -> bool:
        logger.info("Ausführe: %s", action)
        try:
            if action.type == "build_specific":
                return await self._build_specific(action.params)
            elif action.type == "build_storage":
                return await self._build_storage(action.params)
            elif action.type == "build_next_building":
                return await self._build_next_building(action.params)
            elif action.type == "start_research":
                return await self._start_research(action.params)
            elif action.type == "donate_alliance":
                return await self.donate_to_alliance(action.params.get("donations", {}))
            else:
                logger.warning("Unbekannte Aktion: %s", action.type)
                return False
        except Exception as e:
            logger.error("Aktion '%s' fehlgeschlagen: %s", action.type, type(e).__name__)
            return False

    # ------------------------------------------------------------------
    # Allianz-Spende via Browser-UI
    # ------------------------------------------------------------------

    BANK_INPUT_IDS: dict[str, str] = {
        "iron": "bank-iron", "steel": "bank-steel",
        "chemicals": "bank-chemicals", "ice": "bank-ice",
        "water": "bank-water", "energy": "bank-energy",
        "vv4a": "bank-vv4a", "credits": "bank-credits",
    }

    async def donate_to_alliance(self, donations: dict[str, int]) -> bool:
        """Spendet Ressourcen an die Allianz-Kasse via Browser-UI.

        donations: {"iron": 500, "steel": 200, ...}
        Navigiert zu Alliance → Kasse, setzt Betraege, klickt Spenden.
        """
        if not donations:
            return False
        try:
            # 1. Alliance-View oeffnen
            await self._page.evaluate("showView('alliance')")
            await asyncio.sleep(1)

            # 2. Kasse-Tab oeffnen
            await self._page.evaluate("showAllianceSubTab('bank')")
            await asyncio.sleep(1)

            # 3. Alle Input-Felder erst auf 0 setzen, dann Betraege eintragen
            for input_id in self.BANK_INPUT_IDS.values():
                await self._page.evaluate(
                    f"document.getElementById('{input_id}').value = 0"
                )
            for resource, amount in donations.items():
                input_id = self.BANK_INPUT_IDS.get(resource)
                if input_id and amount > 0:
                    await self._page.evaluate(
                        f"document.getElementById('{input_id}').value = {int(amount)}"
                    )

            # 4. Spenden-Button klicken
            await self._page.evaluate("allianceBankAction('donate')")
            await asyncio.sleep(1)

            # 5. Zurueck zur Uebersicht
            await self._page.evaluate("showView('overview')")
            await asyncio.sleep(0.5)

            logger.info("Allianz-Spende via Browser: %s", donations)
            return True

        except Exception as e:
            logger.error("Allianz-Spende fehlgeschlagen: %s", type(e).__name__)
            # Versuch zurueck zur Uebersicht
            try:
                await self._page.evaluate("showView('overview')")
            except Exception:
                pass
            return False

    # ------------------------------------------------------------------
    # Gezielter Bau (beliebiges Gebäude per Building-Type)
    # ------------------------------------------------------------------

    async def _build_specific(self, params: dict) -> bool:
        """Baut ein bestimmtes Gebäude per onclick=startBuild('type')."""
        btype = params.get("building_type", "")
        bname = params.get("building_name", btype)
        reason = params.get("reason", "")

        try:
            await self._page.click("#btn-gebbau")
            await asyncio.sleep(1)

            # Versteckte Gebäude einblenden (falls nötig)
            cb = await self._page.query_selector("#show-hidden-buildings")
            if cb and not await cb.is_checked():
                await cb.click()
                await asyncio.sleep(0.5)

            btn = await self._find_build_button(btype)

            if btn is None:
                logger.warning("Bau '%s': Button nicht gefunden.", btype)
                return False

            if await btn.is_disabled():
                logger.warning("Bau '%s': nicht baubar (Ressourcen/Voraussetzung).", btype)
                return False

            await btn.click()
            await asyncio.sleep(1)
            logger.info("Gebaut: '%s' — %s", bname, reason)
            return True

        except Exception as e:
            logger.error("Bau '%s': %s", btype, type(e).__name__)
            return False

    # ------------------------------------------------------------------
    # Lager-Bau (spezifisches Gebäude per Building-Type)
    # ------------------------------------------------------------------

    async def _build_storage(self, params: dict) -> bool:
        """Baut ein Lagergebäude — delegiert an _build_specific mit angepasstem Log."""
        resource = params.get("resource", "?")
        fill_ratio = params.get("fill_ratio", 0)
        params = dict(params)
        params["reason"] = f"Lager für {resource} ({fill_ratio*100:.0f}% voll)"
        return await self._build_specific(params)

    # ------------------------------------------------------------------
    # Gebäudebau
    # ------------------------------------------------------------------

    async def _build_next_building(self, params: dict) -> bool:
        """Öffnet die Gebäude-Ansicht und klickt den ersten aktiven 'Bauen'-Button."""
        try:
            await self._page.click("#btn-gebbau")
            await asyncio.sleep(1)

            buttons = await self._page.query_selector_all(".build-btn")
            for btn in buttons:
                text = (await btn.inner_text()).strip()
                is_disabled = await btn.is_disabled()
                if text == "Bauen" and not is_disabled:
                    row = await btn.evaluate_handle(
                        "el => el.closest('tr') || el.closest('.build-item') || el.parentElement"
                    )
                    name_el = await row.query_selector(".building-name, td:first-child, .bname")
                    name = (await name_el.inner_text()).strip() if name_el else "Unbekannt"
                    await btn.click()
                    await asyncio.sleep(1)
                    logger.info("Gebäude gestartet: %s", name)
                    return True

            logger.info("Kein baubares Gebäude verfügbar.")
            return False

        except Exception as e:
            logger.error("Gebäudebau: %s", type(e).__name__)
            return False

    # ------------------------------------------------------------------
    # Forschung
    # ------------------------------------------------------------------

    async def _ensure_research_table_tab(self) -> bool:
        """Stellt sicher, dass im Forschungs-View der 'Tabelle'-Tab aktiv ist.

        Wenn stattdessen 'Tech-Baum' aktiv ist, rendert der View keine
        ``<tr>``-Zeilen und der Forschungsstart schlägt fehl. Wir probieren
        der Reihe nach mehrere Strategien durch, bis wieder Zeilen erscheinen.
        """

        async def has_table_rows() -> bool:
            try:
                rows = await self._page.query_selector_all("#view-research tr")
                return len(rows) > 0
            except Exception:
                return False

        if await has_table_rows():
            return True

        # 1) bekannte JS-Helfer des Spiels durchprobieren
        js_candidates = [
            "showResearchSubTab('table')",
            "showResearchSubTab('tabelle')",
            "showResearchSubTab('list')",
            "showResearchTab('table')",
            "showResearchTab('tabelle')",
            "showResearchTab('list')",
            "setResearchView('table')",
            "researchShowTab('table')",
            "switchResearchView('table')",
        ]
        for expr in js_candidates:
            try:
                await self._page.evaluate(expr)
            except Exception:
                continue
            await asyncio.sleep(0.3)
            if await has_table_rows():
                logger.info("Research-Tab gewechselt via JS: %s", expr)
                return True

        # 2) generische DOM-Selektoren für den Tabelle-Tab/-Button
        selector_candidates = [
            "#view-research [data-tab='table']",
            "#view-research [data-view='table']",
            "#view-research .tab-table",
            "#view-research .tab-tabelle",
            "#btn-research-table",
            "#btn-forschung-tabelle",
            "#research-tab-table",
        ]
        for sel in selector_candidates:
            try:
                el = await self._page.query_selector(sel)
                if not el:
                    continue
                await el.click()
            except Exception:
                continue
            await asyncio.sleep(0.3)
            if await has_table_rows():
                logger.info("Research-Tab gewechselt via Selektor: %s", sel)
                return True

        # 3) Playwright text locator als letzte Option
        try:
            loc = self._page.locator("#view-research").get_by_text("Tabelle", exact=True).first
            await loc.click(timeout=1500)
            await asyncio.sleep(0.3)
            if await has_table_rows():
                logger.info("Research-Tab gewechselt via Text-Locator 'Tabelle'.")
                return True
        except Exception:
            pass

        logger.warning(
            "Research-Tab konnte nicht auf 'Tabelle' gewechselt werden — "
            "evtl. ist 'Tech-Baum' aktiv und es gibt keinen Wechsel-Hook."
        )
        return False

    async def _start_research(self, params: dict) -> bool:
        """Öffnet die Forschungs-Ansicht und startet die gewünschte Forschung.

        Sucht die Forschungszeile anhand des Namens (da keine data-rtype Attribute
        im DOM vorhanden sind). Klickt dann den aktiven Button in dieser Zeile.
        """
        research_type = params.get("research_type", "")
        name = params.get("name", research_type)

        try:
            await self._page.click("#btn-forschung")
            await asyncio.sleep(1)

            # Der Forschungs-View hat zwei Tabs: 'Tech-Baum' (Graph) und
            # 'Tabelle' (Liste mit Start-Buttons). Nur im Tabelle-Tab sind
            # <tr>-Zeilen vorhanden — ggf. umschalten.
            await self._ensure_research_table_tab()

            # Alle Tabellenzeilen im Forschungs-View durchsuchen
            rows = await self._page.query_selector_all("#view-research tr")

            for row in rows:
                row_text = (await row.inner_text()).strip()

                # Zeile anhand des Namens identifizieren
                if name.lower() not in row_text.lower() and research_type.lower() not in row_text.lower():
                    continue

                # Button in dieser Zeile finden
                buttons = await row.query_selector_all("button")
                for btn in buttons:
                    btn_text = (await btn.inner_text()).strip()
                    is_disabled = await btn.is_disabled()

                    # Überspringen: Abbrechen-Button, bereits erforscht, Labor belegt
                    skip_texts = {"X", "Erforscht", "Labor belegt", "Zu teuer", "Voraussetzung fehlt"}
                    if btn_text in skip_texts or is_disabled:
                        continue

                    await btn.click()
                    await asyncio.sleep(1)
                    logger.info("Forschung gestartet: '%s'", name)
                    return True

            # Fallback wenn Name-Matching fehlschlägt
            logger.warning("Forschung '%s': Zeile nicht gefunden — Fallback.", name)
            all_buttons = await self._page.query_selector_all("#view-research .build-btn")
            for btn in all_buttons:
                btn_text = (await btn.inner_text()).strip()
                skip_texts = {"X", "Erforscht", "Labor belegt", "Zu teuer", "Voraussetzung fehlt"}
                if btn_text in skip_texts or await btn.is_disabled():
                    continue
                await btn.click()
                await asyncio.sleep(1)
                logger.info("Forschung per Fallback gestartet.")
                return True

            logger.info("Forschung '%s': kein aktiver Button.", name)
            return False

        except Exception as e:
            logger.error("Forschung '%s': %s", name, type(e).__name__)
            return False
