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
            else:
                logger.warning("Unbekannte Aktion: %s", action.type)
                return False
        except Exception as e:
            logger.error("Aktion '%s' fehlgeschlagen: %s", action.type, type(e).__name__)
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
