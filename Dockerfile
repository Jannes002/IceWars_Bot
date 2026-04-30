# ── Stage 1: Playwright + Python ──────────────────────────────────────────────
# Offizielles Playwright-Image bringt Chromium + alle System-Dependencies mit.
FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

# System-Pakete (nur was wirklich fehlt)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Python-Abhängigkeiten zuerst (Layer-Cache)
COPY pyproject.toml setup.cfg ./
RUN pip install --no-cache-dir -e ".[dev]"

# Playwright-Browser vorinstallieren (Chromium reicht)
RUN playwright install chromium

# Quellcode kopieren
COPY icewars_bot/ ./icewars_bot/

# Persistente Verzeichnisse vorbereiten
RUN mkdir -p data logs

# Dashboard-Port
EXPOSE 8050

# tini als PID-1 (sauberes Signal-Handling)
ENTRYPOINT ["/usr/bin/tini", "--"]

# Bot + Dashboard in einem Prozess starten.
# --no-dashboard deaktiviert den Dashboard-Thread (falls gewünscht).
CMD ["python", "-m", "icewars_bot.main", "--headless", "true"]
