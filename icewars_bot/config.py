from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@dataclass
class AuthConfig:
    username: str
    password: str
    game_url: str


@dataclass
class BrowserConfig:
    headless: bool = True
    slow_mo_ms: int = 0
    viewport: dict = field(default_factory=lambda: {"width": 1280, "height": 900})


@dataclass
class BotConfig:
    turn_delay_s: float = 60.0
    action_delay_ms: int = 400
    max_retries: int = 3


@dataclass
class StrategyConfig:
    aggression: str = "balanced"


@dataclass
class TelegramConfig:
    enabled: bool = False
    token: str = ""
    chat_id: str = ""


@dataclass
class Config:
    auth: AuthConfig
    browser: BrowserConfig
    bot: BotConfig
    strategy: StrategyConfig
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            data = tomllib.load(f)

        auth = AuthConfig(**data["auth"])
        browser_data = data.get("browser", {})
        browser = BrowserConfig(
            headless=browser_data.get("headless", True),
            slow_mo_ms=browser_data.get("slow_mo_ms", 0),
            viewport=browser_data.get("viewport", {"width": 1280, "height": 900}),
        )
        bot_data = data.get("bot", {})
        bot = BotConfig(
            turn_delay_s=bot_data.get("turn_delay_s", 60.0),
            action_delay_ms=bot_data.get("action_delay_ms", 400),
            max_retries=bot_data.get("max_retries", 3),
        )
        strategy_data = data.get("strategy", {})
        strategy = StrategyConfig(
            aggression=strategy_data.get("aggression", "balanced"),
        )
        tg_data = data.get("telegram", {})
        telegram = TelegramConfig(
            enabled=tg_data.get("enabled", False),
            token=tg_data.get("token", ""),
            chat_id=str(tg_data.get("chat_id", "")),
        )
        return cls(auth=auth, browser=browser, bot=bot, strategy=strategy,
                   telegram=telegram)
