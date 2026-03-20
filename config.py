"""
Configuration module using Pydantic Settings.
Reads all values from environment variables / .env file.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Polymarket CLOB credentials
    relayer_api_key: str = Field(..., description="Polymarket CLOB relayer API key")
    relayer_api_secret: str = Field("", description="Relayer API secret (for L2 HMAC auth)")
    relayer_api_passphrase: str = Field("", description="Relayer API passphrase")
    relayer_api_address: str = Field(..., description="Relayer signer wallet address (0x807b6...)")
    signer_address: str = Field(..., description="Signer wallet address (same as relayer_api_address)")
    funder_address: str = Field("", description="Account wallet that holds the funds (0xc570...). Distinct from signer when using hosted relayer.")

    # Database
    database_url: str = Field(
        "postgresql+asyncpg://polymarket:password@localhost:5432/polymarket_bot",
        description="Async PostgreSQL connection URL",
    )

    # Telegram
    telegram_bot_token: str = Field(..., description="Telegram bot token from BotFather")
    telegram_admin_id: int = Field(..., description="Telegram user ID of the admin")

    # Budget defaults (can be overridden via DB settings at runtime)
    default_budget_total: float = Field(50.0, description="Total budget in USD")
    default_per_trade_pct: float = Field(5.0, description="Budget % to deploy per trade")
    default_max_trade_usd: float = Field(20.0, description="Hard cap per single trade USD")


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
