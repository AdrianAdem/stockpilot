import os
import sys

from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

load_dotenv()


class AlpacaConfig(BaseModel):
    api_key: str
    secret_key: str
    base_url: str

    @field_validator("base_url")
    @classmethod
    def must_be_paper(cls, v: str) -> str:
        if "paper-api.alpaca.markets" not in v:
            print("FATAL: ALPACA_BASE_URL must point to paper-api.alpaca.markets")
            sys.exit(1)
        return v


class AnthropicConfig(BaseModel):
    api_key: str


class TelegramConfig(BaseModel):
    bot_token: str
    chat_id: str


class RiskConfig(BaseModel):
    max_position_pct: float = 0.03
    max_sector_pct: float = 0.40
    max_portfolio_invested: float = 0.80
    max_positions: int = 15
    daily_drawdown_limit: float = 0.02
    weekly_drawdown_limit: float = 0.05
    min_position_usd: float = 100.0


class StrategyConfig(BaseModel):
    scan_interval_seconds: int = 900
    min_signal_score: float = 0.6
    momentum_weight: float = 0.30
    mean_reversion_weight: float = 0.25
    whale_follow_weight: float = 0.25
    claude_weight: float = 0.20


class Settings(BaseModel):
    alpaca: AlpacaConfig
    anthropic: AnthropicConfig
    telegram: TelegramConfig
    risk: RiskConfig
    strategy: StrategyConfig
    fred_api_key: str
    sec_user_agent: str
    extra_watchlist: list[str]


def load_config() -> Settings:
    extra = os.getenv("EXTRA_WATCHLIST", "")
    watchlist = [s.strip() for s in extra.split(",") if s.strip()]

    return Settings(
        alpaca=AlpacaConfig(
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
            base_url=os.environ["ALPACA_BASE_URL"],
        ),
        anthropic=AnthropicConfig(
            api_key=os.environ["ANTHROPIC_API_KEY"],
        ),
        telegram=TelegramConfig(
            bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            chat_id=os.environ["TELEGRAM_CHAT_ID"],
        ),
        risk=RiskConfig(
            max_position_pct=float(os.getenv("MAX_POSITION_PCT", "0.03")),
            max_sector_pct=float(os.getenv("MAX_SECTOR_PCT", "0.40")),
            max_portfolio_invested=float(os.getenv("MAX_PORTFOLIO_INVESTED", "0.80")),
            max_positions=int(os.getenv("MAX_POSITIONS", "15")),
            daily_drawdown_limit=float(os.getenv("DAILY_DRAWDOWN_LIMIT", "0.02")),
            weekly_drawdown_limit=float(os.getenv("WEEKLY_DRAWDOWN_LIMIT", "0.05")),
            min_position_usd=float(os.getenv("MIN_POSITION_USD", "100")),
        ),
        strategy=StrategyConfig(
            scan_interval_seconds=int(os.getenv("SCAN_INTERVAL_SECONDS", "900")),
            min_signal_score=float(os.getenv("MIN_SIGNAL_SCORE", "0.6")),
            momentum_weight=float(os.getenv("MOMENTUM_WEIGHT", "0.30")),
            mean_reversion_weight=float(os.getenv("MEAN_REVERSION_WEIGHT", "0.25")),
            whale_follow_weight=float(os.getenv("WHALE_FOLLOW_WEIGHT", "0.25")),
            claude_weight=float(os.getenv("CLAUDE_WEIGHT", "0.20")),
        ),
        fred_api_key=os.environ["FRED_API_KEY"],
        sec_user_agent=os.getenv("SEC_USER_AGENT", "stockpilot user@example.com"),
        extra_watchlist=watchlist,
    )
