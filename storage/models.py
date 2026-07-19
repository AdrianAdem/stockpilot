from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    SKIP = "SKIP"


class Signal(BaseModel):
    symbol: str
    action: Action
    score: float = Field(ge=0.0, le=1.0)
    strategy: str
    target_price: float | None = None
    stop_loss_price: float | None = None
    timeframe: str | None = None
    reasoning: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class TradeRecord(BaseModel):
    id: int | None = None
    symbol: str
    side: Side
    qty: int
    price: float
    order_id: str
    strategy: str
    signal_score: float
    stop_loss: float | None = None
    take_profit: float | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    closed_at: datetime | None = None
    close_price: float | None = None
    pnl: float | None = None


class WhaleHolding(BaseModel):
    fund_name: str
    cik: str
    symbol: str
    shares: int
    value_usd: float
    change_type: str  # NEW, INCREASED, DECREASED, UNCHANGED, SOLD
    change_pct: float | None = None
    filing_date: str


class ClaudeAnalysis(BaseModel):
    action: Action
    confidence: float = Field(ge=0.0, le=1.0)
    target_price: float
    stop_loss_price: float
    timeframe: str
    reasoning: str
    risk_factors: list[str] = []
    sector_outlook: str = "neutral"


class DailySummary(BaseModel):
    date: str
    portfolio_value: float
    daily_pnl: float
    daily_pnl_pct: float
    trades_count: int
    buys: int
    sells: int
    stop_losses: int
    win_rate_30d: float
    open_positions: int
    max_positions: int
    top_performer: str | None = None
    top_performer_pct: float | None = None
    worst_performer: str | None = None
    worst_performer_pct: float | None = None
    api_cost_today: float = 0.0


class PositionInfo(BaseModel):
    symbol: str
    qty: int
    entry_price: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    stop_loss: float | None = None
    take_profit: float | None = None
    entry_date: datetime | None = None
    days_held: int = 0
    sector: str = "Unknown"
