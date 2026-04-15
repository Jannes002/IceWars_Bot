"""One-off inspection script: logs in and dumps current buildings + research.

Read-only. Writes results to logs/live_inspect.json for plan verification.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from icewars_bot.auth import Authenticator
from icewars_bot.browser import BrowserManager
from icewars_bot.config import Config


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.load(Path("config.toml"))
    # Force headless for inspection
    config.browser.headless = True

    bm = BrowserManager(config)
    page = await bm.start()
    try:
        auth = Authenticator(page, config)
        if not await auth.ensure_logged_in():
            raise RuntimeError("Login failed")

        # Settle after login — the page may still be navigating/reloading.
        await asyncio.sleep(2.0)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        city = await page.evaluate(
            """async () => {
                const t = localStorage.getItem('icewars_token');
                const r = await fetch('/api/city/', { headers: { 'Authorization': 'Bearer ' + t } });
                return await r.json();
            }"""
        )
        research = await page.evaluate(
            """async () => {
                const t = localStorage.getItem('icewars_token');
                const r = await fetch('/api/research/', { headers: { 'Authorization': 'Bearer ' + t } });
                return await r.json();
            }"""
        )

        # The /api/city/ endpoint returns the city dict directly (no "city" wrapper).
        out = {
            "city_summary": {
                "coords": city.get("coords"),
                "name": city.get("name"),
                "planet_type": city.get("planet_type"),
                "resources": city.get("resources"),
                "rates": city.get("rates"),
                "capacity": city.get("capacity"),
                "fp": city.get("fp"),
                "population_max": city.get("population_max"),
                "population_free": city.get("population_free"),
                "satisfaction": city.get("satisfaction"),
            },
            "raw_city_keys": sorted(list(city.keys())),
            "buildings": [
                {
                    "type": b.get("type"),
                    "name": b.get("name"),
                    "category": b.get("category"),
                    "count": b.get("count"),
                    "level": b.get("level"),
                    "build_time_sec": b.get("build_time_sec"),
                    "can_afford": b.get("can_afford"),
                    "reqs_met": b.get("reqs_met"),
                    "p_restricted": b.get("p_restricted"),
                    "research_missing": b.get("research_missing"),
                    "upgrade_cost": b.get("upgrade_cost"),
                    "next_level_effect": b.get("next_level_effect"),
                }
                for b in (city.get("buildings") or [])
            ],
            "research": [
                {
                    "type": r.get("type"),
                    "name": r.get("name"),
                    "fp_cost": r.get("fp_cost"),
                    "res_cost": r.get("res_cost"),
                    "is_researched": r.get("is_researched"),
                    "has_prereq": r.get("has_prereq"),
                    "can_afford": r.get("can_afford"),
                    "time_sec": r.get("time_sec"),
                    "unlocks_buildings": [
                        u.get("name") for u in (r.get("unlocks_b_data") or [])
                    ],
                }
                for r in (research.get("research") or [])
            ],
        }

        out_path = Path("logs/live_inspect.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {out_path.resolve()}")

        # Print short summary
        building_types = sorted({b["type"] for b in out["buildings"] if b["type"]})
        print(f"Buildings ({len(building_types)}): {', '.join(building_types)}")
        researched = [r["type"] for r in out["research"] if r["is_researched"]]
        pending = [r["type"] for r in out["research"] if not r["is_researched"] and r["has_prereq"]]
        print(f"Researched ({len(researched)}): {', '.join(researched)}")
        print(f"Pending with prereq ({len(pending)}): {', '.join(pending)}")
    finally:
        await bm.stop()


if __name__ == "__main__":
    asyncio.run(main())
