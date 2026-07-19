from pydantic import BaseModel


class MomentumParams(BaseModel):
    rsi_low: float = 40.0
    rsi_high: float = 65.0
    rsi_overbought: float = 75.0
    macd_lookback_days: int = 3
    volume_multiplier: float = 1.5


class MeanReversionParams(BaseModel):
    rsi_oversold: float = 30.0
    min_market_cap_b: float = 10.0  # billion


class WhaleFollowParams(BaseModel):
    min_whale_count: int = 3
    max_rsi: float = 70.0
    hold_days: int = 20


MOMENTUM = MomentumParams()
MEAN_REVERSION = MeanReversionParams()
WHALE_FOLLOW = WhaleFollowParams()
