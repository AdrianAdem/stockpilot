import asyncio

import structlog

from analysis.claude_analyst import ClaudeAnalyst
from storage.models import Action, Signal
from strategy.base import Strategy

logger = structlog.get_logger()


class SignalCombiner:
    """Merges per-strategy signals into one score and adds the LLM verdict.

    Strategy scores are averaged by strategy weight. Candidates that could still
    clear the entry gate are sent to Claude; its confidence contributes
    `weight_claude` of the final score. Symbols with conflicting BUY/SELL
    signals are dropped.
    """

    def __init__(
        self,
        strategies: list[Strategy],
        claude: ClaudeAnalyst,
        weight_claude: float = 0.20,
        min_score: float = 0.65,
        max_claude_calls: int = 12,
    ):
        self.strategies = strategies
        self.claude = claude
        self.weight_claude = weight_claude
        self.min_score = min_score
        # Hard cap on Claude (Haiku+Sonnet) calls per scan to bound token cost.
        self.max_claude_calls = max_claude_calls

    async def generate_combined_signals(
        self, universe: list[str], tech_data: dict, news=None, fred=None, portfolio=None, db=None
    ) -> list[Signal]:
        # Gather signals from all strategies
        all_strategy_signals: dict[str, list[Signal]] = {}

        tasks = [s.generate_signals(universe, tech_data, news=news) for s in self.strategies]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        per_strategy_counts: dict[str, int] = {}
        for strategy, result in zip(self.strategies, results, strict=False):
            if isinstance(result, Exception):
                logger.error("strategy_error", strategy=strategy.name, error=str(result))
                per_strategy_counts[strategy.name] = -1  # errored
                continue
            per_strategy_counts[strategy.name] = len(result)
            for sig in result:
                if sig.symbol not in all_strategy_signals:
                    all_strategy_signals[sig.symbol] = []
                all_strategy_signals[sig.symbol].append(sig)

        # P4: per-strategy signal counts every scan
        logger.info("scan_strategy_counts", **dict(per_strategy_counts.items()))

        combined = []
        macro_data = fred.get_macro_summary() if fred else {}
        portfolio_str = self._format_portfolio(portfolio)

        # Pre-compute strategy_score per symbol, then process highest-first so
        # the limited Claude budget is spent on the strongest candidates.
        scored = []
        for symbol, signals in all_strategy_signals.items():
            actions = {s.action for s in signals}
            if Action.BUY in actions and Action.SELL in actions:
                logger.info("signal_conflict_skip", symbol=symbol)
                continue
            weighted_score = 0.0
            total_weight = 0.0
            best_signal = signals[0]
            for sig in signals:
                strat = next((s for s in self.strategies if s.name == sig.strategy), None)
                w = strat.weight if strat else 0.1
                weighted_score += sig.score * w
                total_weight += w
                if sig.score > best_signal.score:
                    best_signal = sig
            strategy_score = weighted_score / total_weight if total_weight > 0 else 0
            scored.append((strategy_score, symbol, signals, best_signal))

        scored.sort(key=lambda x: x[0], reverse=True)

        # Below this, Claude can't lift a signal over the gate — don't spend tokens.
        claude_gate = max(0.45, (self.min_score - 0.2) / 0.8 - 0.02)
        claude_calls = 0

        for strategy_score, symbol, signals, best_signal in scored:
            if strategy_score > claude_gate and claude_calls < self.max_claude_calls:
                claude_calls += 1
                td = tech_data.get(symbol, {})
                whale_data = await db.get_whale_consensus(symbol) if db else {}
                news_data = news.get_news_for_symbol(symbol) if news else []

                claude_signal = await self.claude.analyze_for_signal(
                    symbol, td, whale_data, news_data, macro_data, portfolio_str
                )
                strat_names = "+".join(sorted(s.strategy for s in signals))
                if claude_signal:
                    # Check action alignment
                    if claude_signal.action != best_signal.action:
                        logger.info(
                            "claude_disagrees",
                            symbol=symbol,
                            strategy_action=best_signal.action.value,
                            claude_action=claude_signal.action.value,
                        )
                        if claude_signal.action == Action.SKIP:
                            continue

                    claude_contrib = claude_signal.score * self.weight_claude
                    strategy_contrib = strategy_score * (1 - self.weight_claude)
                    final_score = strategy_contrib + claude_contrib

                    combined.append(
                        Signal(
                            symbol=symbol,
                            action=best_signal.action,
                            score=round(final_score, 3),
                            strategy=f"combined({strat_names}+claude)",
                            target_price=claude_signal.target_price or best_signal.target_price,
                            stop_loss_price=claude_signal.stop_loss_price
                            or best_signal.stop_loss_price,
                            timeframe=claude_signal.timeframe or best_signal.timeframe,
                            reasoning=f"Strategies: {best_signal.reasoning} | Claude: {claude_signal.reasoning}",
                        )
                    )
                else:
                    # No Claude signal — distinguish API outage from "Claude said skip".
                    if not self.claude.api_healthy:
                        label = f"combined({strat_names}) [CLAUDE OFFLINE]"
                    else:
                        label = f"combined({strat_names}) [no claude confirm]"
                    final_score = strategy_score * 0.90
                    if final_score > 0.5:
                        combined.append(
                            Signal(
                                symbol=symbol,
                                action=best_signal.action,
                                score=round(final_score, 3),
                                strategy=label,
                                target_price=best_signal.target_price,
                                stop_loss_price=best_signal.stop_loss_price,
                                timeframe=best_signal.timeframe,
                                reasoning=best_signal.reasoning,
                            )
                        )
            elif strategy_score > claude_gate:
                # Above the gate but Claude budget spent this scan — still allow
                # a strong strategy-only signal through (discounted).
                strat_names = "+".join(sorted(s.strategy for s in signals))
                final_score = strategy_score * 0.90
                if final_score > 0.5:
                    combined.append(
                        Signal(
                            symbol=symbol,
                            action=best_signal.action,
                            score=round(final_score, 3),
                            strategy=f"combined({strat_names}) [claude budget spent]",
                            target_price=best_signal.target_price,
                            stop_loss_price=best_signal.stop_loss_price,
                            timeframe=best_signal.timeframe,
                            reasoning=best_signal.reasoning,
                        )
                    )

        combined.sort(key=lambda s: s.score, reverse=True)
        logger.info(
            "combined_signals", count=len(combined), top=[(s.symbol, s.score) for s in combined[:5]]
        )
        return combined

    def _format_portfolio(self, positions: list[dict] | None) -> str:
        if not positions:
            return "No current positions"
        lines = []
        for p in positions[:10]:
            sym = p.get("symbol", "?")
            qty = p.get("qty", "?")
            pnl = p.get("unrealized_plpc", "?")
            lines.append(f"{sym}: {qty} shares, PnL: {pnl}%")
        return "; ".join(lines)
