import asyncio
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd
import structlog

from data.alpaca_client import AlpacaClient
from data.technical import TechnicalAnalysis
from storage.models import Action, Signal
from strategy.base import Strategy

logger = structlog.get_logger()


@dataclass
class BacktestTrade:
    symbol: str
    side: str
    qty: int
    entry_price: float
    entry_date: str
    exit_price: float = 0.0
    exit_date: str = ""
    pnl: float = 0.0
    strategy: str = ""


@dataclass
class BacktestResult:
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float
    total_return_pct: float
    cagr: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    avg_win: float
    avg_loss: float
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    drawdown_curve: list[float] = field(default_factory=list)


class BacktestEngine:
    def __init__(self, initial_capital: float = 100000.0,
                 slippage: float = 0.0005,
                 max_position_pct: float = 0.03):
        self.initial_capital = initial_capital
        self.slippage = slippage
        self.max_position_pct = max_position_pct
        self.technical = TechnicalAnalysis()

    async def run(self, strategy: Strategy, symbols: list[str],
                  bars_data: dict[str, pd.DataFrame],
                  lookback: int = 200) -> BacktestResult:
        capital = self.initial_capital
        positions: dict[str, BacktestTrade] = {}
        trades: list[BacktestTrade] = []
        equity_curve = [capital]

        # Get all unique dates across symbols
        all_dates = set()
        for df in bars_data.values():
            if not df.empty:
                all_dates.update(df.index.strftime("%Y-%m-%d").tolist())
        dates = sorted(all_dates)

        if not dates:
            return self._empty_result()

        for i, date in enumerate(dates):
            if i < lookback:
                equity_curve.append(capital)
                continue

            # Calculate technicals up to this date
            tech_data = {}
            for symbol in symbols:
                df = bars_data.get(symbol)
                if df is None or df.empty:
                    continue
                mask = df.index <= pd.Timestamp(date, tz='UTC')
                historical = df[mask].tail(lookback)
                if len(historical) >= 50:
                    tech_data[symbol] = self.technical.calculate_all(historical)

            # Generate signals
            try:
                signals = await strategy.generate_signals(symbols, tech_data)
            except Exception:
                signals = []

            # Execute signals
            for sig in signals:
                if sig.score < 0.5:
                    continue

                price = tech_data.get(sig.symbol, {}).get("price")
                if not price:
                    continue

                if sig.action == Action.BUY and sig.symbol not in positions:
                    # Buy
                    entry_price = price * (1 + self.slippage)
                    max_invest = capital * self.max_position_pct
                    qty = int(max_invest / entry_price)
                    if qty > 0 and qty * entry_price <= capital:
                        cost = qty * entry_price
                        capital -= cost
                        positions[sig.symbol] = BacktestTrade(
                            symbol=sig.symbol,
                            side="BUY",
                            qty=qty,
                            entry_price=entry_price,
                            entry_date=date,
                            strategy=strategy.name,
                        )

                elif sig.action == Action.SELL and sig.symbol in positions:
                    # Sell
                    pos = positions.pop(sig.symbol)
                    exit_price = price * (1 - self.slippage)
                    pos.exit_price = exit_price
                    pos.exit_date = date
                    pos.pnl = (exit_price - pos.entry_price) * pos.qty
                    capital += pos.qty * exit_price
                    trades.append(pos)

            # Check stop-losses
            for symbol in list(positions.keys()):
                pos = positions[symbol]
                price = tech_data.get(symbol, {}).get("price")
                if not price:
                    continue
                atr = tech_data.get(symbol, {}).get("ATR", 0)
                stop = pos.entry_price - 2 * atr if atr else pos.entry_price * 0.95
                if price <= stop:
                    exit_price = price * (1 - self.slippage)
                    pos.exit_price = exit_price
                    pos.exit_date = date
                    pos.pnl = (exit_price - pos.entry_price) * pos.qty
                    capital += pos.qty * exit_price
                    trades.append(pos)
                    del positions[symbol]

            # Mark-to-market
            portfolio_value = capital
            for sym, pos in positions.items():
                price = tech_data.get(sym, {}).get("price", pos.entry_price)
                portfolio_value += pos.qty * price
            equity_curve.append(portfolio_value)

        # Close remaining positions at last price
        for symbol, pos in positions.items():
            df = bars_data.get(symbol)
            if df is not None and not df.empty:
                last_price = df["close"].iloc[-1]
                pos.exit_price = last_price
                pos.exit_date = dates[-1]
                pos.pnl = (last_price - pos.entry_price) * pos.qty
                capital += pos.qty * last_price
                trades.append(pos)

        return self._calculate_metrics(trades, equity_curve, dates)

    def _calculate_metrics(self, trades: list[BacktestTrade],
                           equity_curve: list[float],
                           dates: list[str]) -> BacktestResult:
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]

        total_return = (equity_curve[-1] - self.initial_capital) / self.initial_capital
        days = len(dates) if dates else 1
        years = days / 252
        cagr = ((equity_curve[-1] / self.initial_capital) ** (1 / max(years, 0.01))) - 1

        # Sharpe (simplified: daily returns)
        returns = []
        for i in range(1, len(equity_curve)):
            if equity_curve[i - 1] > 0:
                returns.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
        if returns:
            import statistics
            avg_ret = statistics.mean(returns)
            std_ret = statistics.stdev(returns) if len(returns) > 1 else 0.01
            sharpe = (avg_ret / std_ret) * (252 ** 0.5) if std_ret > 0 else 0
        else:
            sharpe = 0

        # Max drawdown
        peak = equity_curve[0]
        max_dd = 0
        drawdown_curve = []
        for val in equity_curve:
            peak = max(peak, val)
            dd = (val - peak) / peak if peak > 0 else 0
            max_dd = min(max_dd, dd)
            drawdown_curve.append(dd)

        # Profit factor
        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        return BacktestResult(
            start_date=dates[0] if dates else "",
            end_date=dates[-1] if dates else "",
            initial_capital=self.initial_capital,
            final_capital=equity_curve[-1],
            total_return_pct=round(total_return * 100, 2),
            cagr=round(cagr * 100, 2),
            sharpe_ratio=round(sharpe, 2),
            max_drawdown=round(max_dd * 100, 2),
            win_rate=round(len(wins) / len(trades) * 100, 1) if trades else 0,
            profit_factor=round(profit_factor, 2),
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            avg_win=round(sum(t.pnl for t in wins) / len(wins), 2) if wins else 0,
            avg_loss=round(sum(t.pnl for t in losses) / len(losses), 2) if losses else 0,
            trades=trades,
            equity_curve=equity_curve,
            drawdown_curve=drawdown_curve,
        )

    async def run_combined(self, strategies: list[Strategy], symbols: list[str],
                           bars_data: dict[str, pd.DataFrame],
                           min_score: float = 0.5,
                           lookback: int = 200) -> BacktestResult:
        capital = self.initial_capital
        positions: dict[str, BacktestTrade] = {}
        trades: list[BacktestTrade] = []
        equity_curve = [capital]

        all_dates = set()
        for df in bars_data.values():
            if not df.empty:
                all_dates.update(df.index.strftime("%Y-%m-%d").tolist())
        dates = sorted(all_dates)

        if not dates:
            return self._empty_result()

        def bar_for(symbol: str, d: str):
            df = bars_data.get(symbol)
            if df is None or df.empty:
                return None
            rows = df[df.index.strftime("%Y-%m-%d") == d]
            return rows.iloc[0] if len(rows) else None

        # No lookahead: signals computed on day i fill at day i+1's OPEN.
        pending: list[Signal] = []

        for i, date in enumerate(dates):
            if i < lookback:
                equity_curve.append(capital)
                continue

            # 1. Execute yesterday's signals at TODAY'S OPEN
            for sig in pending:
                bar = bar_for(sig.symbol, date)
                if bar is None:
                    continue
                open_px = float(bar["open"])
                if sig.action == Action.BUY and sig.symbol not in positions:
                    entry_price = open_px * (1 + self.slippage)
                    max_invest = capital * self.max_position_pct
                    qty = int(max_invest / entry_price)
                    if qty > 0 and qty * entry_price <= capital:
                        capital -= qty * entry_price
                        positions[sig.symbol] = BacktestTrade(
                            symbol=sig.symbol, side="BUY", qty=qty,
                            entry_price=entry_price, entry_date=date,
                            strategy="combined",
                        )
                        # carry the ATR stop computed at signal time
                        positions[sig.symbol].stop = sig.stop_loss_price
                elif sig.action == Action.SELL and sig.symbol in positions:
                    pos = positions.pop(sig.symbol)
                    exit_price = open_px * (1 - self.slippage)
                    pos.exit_price = exit_price
                    pos.exit_date = date
                    pos.pnl = (exit_price - pos.entry_price) * pos.qty
                    capital += pos.qty * exit_price
                    trades.append(pos)
            pending = []

            # 2. Intraday stop check using today's LOW (conservative: if low
            #    breached the stop, assume fill AT the stop price)
            for symbol in list(positions.keys()):
                pos = positions[symbol]
                bar = bar_for(symbol, date)
                if bar is None:
                    continue
                stop = getattr(pos, "stop", None) or pos.entry_price * 0.95
                if float(bar["low"]) <= stop:
                    exit_price = stop * (1 - self.slippage)
                    pos.exit_price = exit_price
                    pos.exit_date = date
                    pos.pnl = (exit_price - pos.entry_price) * pos.qty
                    capital += pos.qty * exit_price
                    trades.append(pos)
                    del positions[symbol]

            # 3. Compute signals from data up to today's close (fills tomorrow)
            tech_data = {}
            for symbol in symbols:
                df = bars_data.get(symbol)
                if df is None or df.empty:
                    continue
                mask = df.index <= pd.Timestamp(date, tz='UTC')
                historical = df[mask].tail(lookback)
                if len(historical) >= 50:
                    tech_data[symbol] = self.technical.calculate_all(historical)

            signal_map: dict[str, list[Signal]] = {}
            for strategy in strategies:
                try:
                    sigs = await strategy.generate_signals(symbols, tech_data)
                    for sig in sigs:
                        if sig.symbol not in signal_map:
                            signal_map[sig.symbol] = []
                        signal_map[sig.symbol].append(sig)
                except Exception:
                    continue

            for symbol, sigs in signal_map.items():
                actions = set(s.action for s in sigs)
                if Action.BUY in actions and Action.SELL in actions:
                    continue
                total_w = sum(
                    next((st.weight for st in strategies if st.name == s.strategy), 0.25)
                    for s in sigs
                )
                weighted = sum(
                    s.score * next((st.weight for st in strategies if st.name == s.strategy), 0.25)
                    for s in sigs
                )
                score = weighted / total_w if total_w > 0 else 0
                if score < min_score:
                    continue
                best = max(sigs, key=lambda s: s.score)
                pending.append(Signal(
                    symbol=symbol,
                    action=best.action,
                    score=round(score, 3),
                    strategy="combined",
                    stop_loss_price=best.stop_loss_price,
                    target_price=best.target_price,
                ))

            # 4. Mark-to-market at today's close
            portfolio_value = capital
            for sym, pos in positions.items():
                bar = bar_for(sym, date)
                p = float(bar["close"]) if bar is not None else pos.entry_price
                portfolio_value += pos.qty * p
            equity_curve.append(portfolio_value)

        for symbol, pos in positions.items():
            df = bars_data.get(symbol)
            if df is not None and not df.empty:
                last_price = df["close"].iloc[-1]
                pos.exit_price = last_price
                pos.exit_date = dates[-1]
                pos.pnl = (last_price - pos.entry_price) * pos.qty
                capital += pos.qty * last_price
                trades.append(pos)

        return self._calculate_metrics(trades, equity_curve, dates)

    def _empty_result(self) -> BacktestResult:
        return BacktestResult(
            start_date="", end_date="",
            initial_capital=self.initial_capital,
            final_capital=self.initial_capital,
            total_return_pct=0, cagr=0, sharpe_ratio=0,
            max_drawdown=0, win_rate=0, profit_factor=0,
            total_trades=0, winning_trades=0, losing_trades=0,
            avg_win=0, avg_loss=0,
        )
