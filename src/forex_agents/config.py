from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_base_url: str = "https://9r.grifyer.my.id/v1"
    llm_api_key: str = ""
    llm_model: str = "Tier2"
    llm_temperature: float = 0.1

    database_url: str = ""

    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = ""

    market_provider: str = "yfinance"
    market_fallback: str = ""
    default_symbol: str = "EURUSD"
    default_timeframe: str = "m1"

    # MetaTrader 5 (MARKET_PROVIDER=mt5)
    mt5_host: str = "127.0.0.1"
    mt5_port: int = 18812
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_path: str = ""
    mt5_timeout: int = 300
    mt5_symbol_suffix: str = ""

    min_rr: float = Field(default=1.5, ge=0.5)
    max_spread_pips: float = 2.0

    @property
    def allowed_user_ids(self) -> set[int]:
        out: set[int] = set()
        for chunk in self.telegram_allowed_user_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                out.add(int(chunk))
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
