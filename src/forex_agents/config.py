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

    # Model per-tahap. Kosong = pakai llm_model. Tahap berat (keputusan entry)
    # sebaiknya pakai model kuat; tahap ringan (tren/validasi/area) murah.
    llm_model_trend: str = ""
    llm_model_validation: str = ""
    llm_model_critical: str = ""
    llm_model_decision: str = ""

    # Fallback bila model utama tahap gagal (kuota habis / model tidak ada).
    # Daftar dipisah koma, mis. "gemini/gemini-3.5-flash-lite,Free".
    llm_fallback_trend: str = ""
    llm_fallback_validation: str = ""
    llm_fallback_critical: str = ""
    llm_fallback_decision: str = ""
    # Fallback terakhir untuk semua tahap.
    llm_fallbacks: str = ""

    database_url: str = ""

    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = ""

    market_provider: str = "yfinance"
    market_fallback: str = ""
    default_symbol: str = "EURUSD"
    default_timeframe: str = "m1"
    default_mode: str = "cascade"

    # TradingView (MARKET_PROVIDER=tradingview)
    tv_exchange: str = "FX"
    tv_server: str = "data"
    tv_timeout: float = 25.0
    tv_range: int = 300
    tv_retries: int = 3
    tv_debug: bool = False

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

    def model_for(self, stage: str) -> str:
        """Model untuk satu tahap: trend/validation/critical/decision."""
        mapping = {
            "trend": self.llm_model_trend,
            "validation": self.llm_model_validation,
            "critical": self.llm_model_critical,
            "decision": self.llm_model_decision,
        }
        return (mapping.get(stage) or "").strip() or self.llm_model

    def fallbacks_for(self, stage: str) -> list[str]:
        """Rantai fallback tahap + rantai global, tanpa duplikat."""
        stage_map = {
            "trend": self.llm_fallback_trend,
            "validation": self.llm_fallback_validation,
            "critical": self.llm_fallback_critical,
            "decision": self.llm_fallback_decision,
        }
        out: list[str] = []
        for raw in (stage_map.get(stage, ""), self.llm_fallbacks):
            for name in (raw or "").replace(";", ",").split(","):
                clean = name.strip()
                if clean and clean not in out and clean != self.model_for(stage):
                    out.append(clean)
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
