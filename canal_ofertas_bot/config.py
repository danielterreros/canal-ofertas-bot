from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(ValueError):
    pass


# Optional settings use these defaults when omitted from the environment.
DEFAULTS = {
    "DATA_DIR": "/data", "TZ": "Europe/Madrid",
    "PREVIEW_TTL_SECONDS": "86400", "ENABLE_PUBLISH": "true",
    "AMAZON_FETCH_HTML": "true", "SCHEDULE_MAX_DELAY_SECONDS": "900",
    "SCHEDULER_INTERVAL_SECONDS": "5", "SCHEDULE_HISTORY_DAYS": "0",
    "HTTP_TIMEOUT_SECONDS": "20", "TELEGRAM_POLL_TIMEOUT_SECONDS": "25",
    "TELEGRAM_RETRY_LIMIT": "3", "MAX_DOWNLOAD_BYTES": "5000000",
    "MAX_REDIRECTS": "6", "MAX_IMAGE_PIXELS": "20000000",
    "IMAGE_BRAND_TEXT": "", "PUBLICATION_FOOTER": "",
    "IMAGE_BRAND_COLOR": "#172433", "IMAGE_CURRENT_PRICE_COLOR": "#008940",
    "IMAGE_PREVIOUS_PRICE_COLOR": "#737b83", "IMAGE_DISCOUNT_BADGE_COLOR": "#e34538",
    "IMAGE_DEAL_TYPE_BADGE_COLOR": "#188451",
    "SHOW_PROMOTION_IN_IMAGE": "true", "SHOW_PROMOTION_IN_CAPTION": "true",
    "SCHEDULE_PERMISSIONS": "own", "REPEAT_WARNING_DAYS": "7",
    "FONT_DIR": "/usr/share/fonts/truetype/dejavu", "LOG_LEVEL": "INFO",
}
REQUIRED = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "TELEGRAM_CHANNEL_ID")


@dataclass(frozen=True)
class Config:
    values: Mapping[str, str] = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        source = os.environ if env is None else env
        values = {key: source.get(key, default) for key, default in DEFAULTS.items()}
        for key in REQUIRED:
            value = source.get(key, "").strip()
            if not value or "RELLENA" in value or "SUSTITUIR" in value:
                raise ConfigurationError(f"Falta {key}: configúrala en .env, en el entorno o en Docker Compose.")
            values[key] = value
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", values["TELEGRAM_BOT_TOKEN"]):
            raise ConfigurationError("TELEGRAM_BOT_TOKEN no tiene el formato de un token de BotFather.")
        owners = values["TELEGRAM_OWNER_ID"].split(",")
        if any(not re.fullmatch(r"[1-9][0-9]*", owner.strip()) for owner in owners):
            raise ConfigurationError("TELEGRAM_OWNER_ID debe contener uno o más ID positivos separados por comas.")
        if len({int(owner.strip()) for owner in owners}) != len(owners):
            raise ConfigurationError("TELEGRAM_OWNER_ID no debe repetir ID.")
        if not re.fullmatch(r"-[1-9][0-9]*", values["TELEGRAM_CHANNEL_ID"]):
            raise ConfigurationError("TELEGRAM_CHANNEL_ID debe ser el ID numérico negativo del grupo, supergrupo o canal de destino.")
        bounds = {
            "PREVIEW_TTL_SECONDS": (60, 2592000), "SCHEDULE_MAX_DELAY_SECONDS": (0, 86400),
            "SCHEDULER_INTERVAL_SECONDS": (1, 60), "SCHEDULE_HISTORY_DAYS": (0, 30),
            "HTTP_TIMEOUT_SECONDS": (5, 60), "TELEGRAM_POLL_TIMEOUT_SECONDS": (1, 50),
            "TELEGRAM_RETRY_LIMIT": (0, 10), "MAX_DOWNLOAD_BYTES": (10000, 10000000),
            "MAX_REDIRECTS": (0, 10), "MAX_IMAGE_PIXELS": (1000000, 40000000),
            "REPEAT_WARNING_DAYS": (0, 30),
        }
        for key, (low, high) in bounds.items():
            try:
                number = int(values[key])
            except (ValueError, TypeError):
                raise ConfigurationError(f"{key} debe ser un entero entre {low} y {high}.") from None
            if not low <= number <= high:
                raise ConfigurationError(f"{key} debe estar entre {low} y {high}.")
        for key in ("ENABLE_PUBLISH", "AMAZON_FETCH_HTML", "SHOW_PROMOTION_IN_IMAGE", "SHOW_PROMOTION_IN_CAPTION"):
            if values[key].lower() not in ("true", "false"):
                raise ConfigurationError(f"{key} debe ser true o false.")
        for key in DEFAULTS:
            if key.endswith("_COLOR") and not re.fullmatch(r"#[0-9a-fA-F]{6}", values[key]):
                raise ConfigurationError(f"{key} debe tener formato #RRGGBB.")
        try:
            ZoneInfo(values["TZ"])
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ConfigurationError("TZ debe ser una zona horaria IANA válida, por ejemplo Europe/Madrid o America/New_York.") from None
        if not Path(values["DATA_DIR"]).is_absolute() or not Path(values["FONT_DIR"]).is_absolute():
            raise ConfigurationError("DATA_DIR y FONT_DIR deben ser rutas absolutas internas del contenedor.")
        if len(values["IMAGE_BRAND_TEXT"]) > 80 or "\n" in values["IMAGE_BRAND_TEXT"]:
            raise ConfigurationError("IMAGE_BRAND_TEXT admite hasta 80 caracteres en una sola línea.")
        if values["LOG_LEVEL"] not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigurationError("LOG_LEVEL debe ser DEBUG, INFO, WARNING o ERROR.")
        if values["SCHEDULE_PERMISSIONS"] not in ("own", "all"):
            raise ConfigurationError("SCHEDULE_PERMISSIONS debe ser own o all.")
        from .footer import parse_footer
        parse_footer(values["PUBLICATION_FOOTER"])
        return cls(values)

    def __getitem__(self, key: str) -> str:
        return self.values[key]

    def integer(self, key: str) -> int:
        return int(self[key])

    def enabled(self, key: str) -> bool:
        return self[key].lower() == "true"

    @property
    def owner(self) -> int:
        return self.owners[0]

    @property
    def owners(self) -> tuple[int, ...]:
        return tuple(int(owner.strip()) for owner in self["TELEGRAM_OWNER_ID"].split(","))

    @property
    def shared_schedule(self) -> bool:
        return self["SCHEDULE_PERMISSIONS"] == "all"

    @property
    def channel(self) -> str:
        return self["TELEGRAM_CHANNEL_ID"]

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self["TZ"])
