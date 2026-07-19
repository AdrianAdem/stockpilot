import json

import anthropic
import structlog

from storage.db import Database
from storage.models import Action, ClaudeAnalysis, Signal

logger = structlog.get_logger()

# Model IDs — bare aliases, never date-suffixed (suffixes 404).
# 2-tier by design: cheap Haiku screen, Sonnet deep-dive only on candidates.
HAIKU_MODEL = "claude-haiku-4-5"
SONNET_MODEL = "claude-sonnet-4-6"

# Pricing per 1M tokens (input, output)
PRICING = {
    "haiku": (1.00, 5.00),
    "sonnet": (3.00, 15.00),
}

HAIKU_SYSTEM = """You are a stock screening filter. Evaluate whether this stock currently has a trading setup. Respond ONLY with JSON.

{"tradeable": true/false, "bias": "long"/"short"/"neutral", "score": 1-10}

tradeable=true only when clear setup is recognizable. Be strict."""

# Static instructions only — keeps the cached prefix byte-identical across symbols.
# Per-symbol data goes in the user message so prompt caching actually hits.
SONNET_SYSTEM = """You are a quantitative stock analyst.

You will receive a stock's symbol, current price, technical indicators, whale (13F) activity, recent news, macro environment, and the current portfolio. Produce a trading recommendation.

Task:
1. Evaluate the technical situation (trend, support/resistance, momentum)
2. Evaluate the fundamental situation based on news and whale activity
3. Evaluate macro risk (VIX level, Fed policy, sector rotation)
4. Give a final recommendation

Respond ONLY with JSON:
{
  "action": "BUY" | "SELL" | "HOLD" | "SKIP",
  "confidence": 0.0-1.0,
  "target_price": XX.XX,
  "stop_loss_price": XX.XX,
  "timeframe": "days" | "weeks",
  "reasoning": "2-3 sentences",
  "risk_factors": ["Risk 1", "Risk 2"],
  "sector_outlook": "bullish" | "neutral" | "bearish"
}

Rules:
- BUY only at confidence > 0.7
- Always specify a stop-loss (based on ATR)
- If macro environment is bearish (VIX > 25): confidence automatically -0.15
- Max 40% of portfolio in one sector
- SKIP if unclear"""


class ClaudeAnalyst:
    """Two-tier LLM analysis: a cheap Haiku screen in front of Sonnet.

    Tracks API health so an outage is reported explicitly instead of silently
    degrading into technical-only signals, and logs token cost per call.
    """

    def __init__(self, api_key: str, db: Database | None = None):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.db = db
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        # Health: True while the last API call succeeded. False after an
        # API-level failure (auth/rate/timeout/connection) — distinct from
        # Claude logically judging a stock not-tradeable.
        self.api_healthy = True
        self.last_error: str | None = None

    async def _log_failure(self, model: str, error: str):
        """Mark Claude unhealthy and persist the failed call."""
        self.api_healthy = False
        self.last_error = error
        if self.db:
            try:
                await self.db.log_api_cost(model, 0, 0, 0.0, success=False, error=error[:300])
            except Exception:
                pass

    async def quick_screen(self, symbol: str, tech_data: dict) -> dict | None:
        prompt = f"""Stock: {symbol}
Price: ${tech_data.get("price", "?")}
RSI: {tech_data.get("RSI", "?")}
MACD: {tech_data.get("MACD_crossover", "?")}
Above SMA50: {tech_data.get("above_SMA50", "?")}
Above SMA200: {tech_data.get("above_SMA200", "?")}
Volume Ratio: {tech_data.get("volume_ratio", "?")}
BB Position: {tech_data.get("BB_position", "?")}"""

        try:
            resp = await self.client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=200,
                system=[
                    {"type": "text", "text": HAIKU_SYSTEM, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": prompt}],
            )
            await self._track_usage(resp, "haiku")
            self.api_healthy = True
            return self._parse_json(resp.content[0].text)
        except anthropic.APIError as e:
            # Real API failure (auth/rate/timeout/connection) — loud, tracked.
            logger.error(
                "claude_api_failure",
                model="haiku",
                symbol=symbol,
                error_type=type(e).__name__,
                error=str(e),
            )
            await self._log_failure("haiku", f"{type(e).__name__}: {e}")
            return None
        except Exception as e:
            logger.error("haiku_screen_error", symbol=symbol, error=str(e))
            return None

    async def deep_analysis(
        self,
        symbol: str,
        tech_data: dict,
        whale_data: dict,
        news: list[dict],
        macro_data: dict,
        portfolio_context: str = "",
    ) -> ClaudeAnalysis | None:
        # Per-symbol data in the user turn — the static SONNET_SYSTEM stays cached.
        user_content = f"""Stock: {symbol}
Current Price: ${tech_data.get("price", "?")}
Technical Data: {json.dumps(tech_data, default=str)}
Whale Activity: {json.dumps(whale_data, default=str)}
Recent News: {json.dumps(news[:5], default=str) if news else "No recent news"}
Macro Environment: {json.dumps(macro_data, default=str)}
Current Portfolio Positions: {portfolio_context or "No current positions"}

Analyze this stock and provide your recommendation."""

        try:
            resp = await self.client.messages.create(
                model=SONNET_MODEL,
                max_tokens=500,
                system=[
                    {"type": "text", "text": SONNET_SYSTEM, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": user_content}],
            )
            await self._track_usage(resp, "sonnet")
            self.api_healthy = True
            data = self._parse_json(resp.content[0].text)
            if not data:
                return None

            return ClaudeAnalysis(
                action=Action(data.get("action", "SKIP")),
                confidence=float(data.get("confidence", 0)),
                target_price=float(data.get("target_price", 0)),
                stop_loss_price=float(data.get("stop_loss_price", 0)),
                timeframe=data.get("timeframe", "days"),
                reasoning=data.get("reasoning", ""),
                risk_factors=data.get("risk_factors", []),
                sector_outlook=data.get("sector_outlook", "neutral"),
            )
        except anthropic.APIError as e:
            logger.error(
                "claude_api_failure",
                model="sonnet",
                symbol=symbol,
                error_type=type(e).__name__,
                error=str(e),
            )
            await self._log_failure("sonnet", f"{type(e).__name__}: {e}")
            return None
        except Exception as e:
            logger.error("sonnet_analysis_error", symbol=symbol, error=str(e))
            return None

    def _parse_json(self, text: str) -> dict | None:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON embedded in text
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass
            logger.warning("json_parse_failed", text=text[:200])
            return None

    async def _track_usage(self, response, model: str):
        usage = response.usage
        self.total_input_tokens += usage.input_tokens
        self.total_output_tokens += usage.output_tokens

        in_price, out_price = PRICING[model]
        cost = (usage.input_tokens * in_price + usage.output_tokens * out_price) / 1_000_000

        if self.db:
            await self.db.log_api_cost(model, usage.input_tokens, usage.output_tokens, cost)

        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        logger.debug(
            "claude_usage",
            model=model,
            input=usage.input_tokens,
            output=usage.output_tokens,
            cache_read=cache_read,
            cost=f"${cost:.4f}",
        )

    async def analyze_for_signal(
        self,
        symbol: str,
        tech_data: dict,
        whale_data: dict,
        news: list[dict],
        macro_data: dict,
        portfolio_context: str = "",
    ) -> Signal | None:
        # Tier 1: Quick screen
        screen = await self.quick_screen(symbol, tech_data)
        if not screen or not screen.get("tradeable"):
            return None

        # Tier 2: Deep analysis
        analysis = await self.deep_analysis(
            symbol, tech_data, whale_data, news, macro_data, portfolio_context
        )
        if not analysis:
            return None

        if analysis.action == Action.SKIP or analysis.confidence < 0.5:
            return None

        return Signal(
            symbol=symbol,
            action=analysis.action,
            score=analysis.confidence,
            strategy="claude_analyst",
            target_price=analysis.target_price,
            stop_loss_price=analysis.stop_loss_price,
            timeframe=analysis.timeframe,
            reasoning=analysis.reasoning,
        )
