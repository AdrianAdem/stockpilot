from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import structlog

from storage.models import DailySummary, TradeRecord, WhaleHolding

logger = structlog.get_logger()

DB_PATH = Path(__file__).parent.parent / "data" / "stockpilot.db"


class Database:
    """Async SQLite store for trades, signals, API costs and equity snapshots."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path or DB_PATH)
        self._db: aiosqlite.Connection | None = None

    async def connect(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()
        await self._migrate()
        logger.info("db_connected", path=self.db_path)

    async def _migrate(self):
        # Add columns to existing DBs created before the schema grew.
        cursor = await self._db.execute("PRAGMA table_info(api_costs)")
        cols = {r[1] for r in await cursor.fetchall()}
        if "success" not in cols:
            await self._db.execute(
                "ALTER TABLE api_costs ADD COLUMN success INTEGER NOT NULL DEFAULT 1"
            )
        if "error" not in cols:
            await self._db.execute("ALTER TABLE api_costs ADD COLUMN error TEXT")
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    async def _create_tables(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty INTEGER NOT NULL,
                price REAL NOT NULL,
                order_id TEXT UNIQUE NOT NULL,
                strategy TEXT NOT NULL,
                signal_score REAL NOT NULL,
                stop_loss REAL,
                take_profit REAL,
                timestamp TEXT NOT NULL,
                closed_at TEXT,
                close_price REAL,
                pnl REAL
            );

            CREATE TABLE IF NOT EXISTS whale_holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fund_name TEXT NOT NULL,
                cik TEXT NOT NULL,
                symbol TEXT NOT NULL,
                shares INTEGER NOT NULL,
                value_usd REAL NOT NULL,
                change_type TEXT NOT NULL,
                change_pct REAL,
                filing_date TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                UNIQUE(cik, symbol, filing_date)
            );

            CREATE TABLE IF NOT EXISTS signals_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                score REAL NOT NULL,
                strategy TEXT NOT NULL,
                reasoning TEXT,
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                timestamp TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 1,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                portfolio_value REAL NOT NULL,
                cash REAL NOT NULL,
                positions_count INTEGER NOT NULL,
                timestamp TEXT NOT NULL
            );
        """)
        await self._db.commit()

    async def log_trade(self, trade: TradeRecord):
        await self._db.execute(
            """INSERT OR IGNORE INTO trades
            (symbol, side, qty, price, order_id, strategy, signal_score,
             stop_loss, take_profit, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.symbol,
                trade.side.value,
                trade.qty,
                trade.price,
                trade.order_id,
                trade.strategy,
                trade.signal_score,
                trade.stop_loss,
                trade.take_profit,
                trade.timestamp.isoformat(),
            ),
        )
        await self._db.commit()

    async def close_trade(self, order_id: str, close_price: float, pnl: float):
        await self._db.execute(
            """UPDATE trades SET closed_at=?, close_price=?, pnl=?
            WHERE order_id=?""",
            (datetime.utcnow().isoformat(), close_price, pnl, order_id),
        )
        await self._db.commit()

    async def update_stop_loss(self, symbol: str, stop_loss: float):
        """Update the recorded stop for the open trade of a symbol."""
        await self._db.execute(
            "UPDATE trades SET stop_loss=? WHERE symbol=? AND closed_at IS NULL",
            (stop_loss, symbol),
        )
        await self._db.commit()

    async def get_open_trades(self) -> list[TradeRecord]:
        cursor = await self._db.execute(
            "SELECT * FROM trades WHERE closed_at IS NULL ORDER BY timestamp DESC"
        )
        rows = await cursor.fetchall()
        return [TradeRecord(**dict(r)) for r in rows]

    async def get_recent_trades(self, days: int = 30) -> list[TradeRecord]:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        cursor = await self._db.execute(
            "SELECT * FROM trades WHERE timestamp > ? ORDER BY timestamp DESC",
            (since,),
        )
        rows = await cursor.fetchall()
        return [TradeRecord(**dict(r)) for r in rows]

    async def log_signal(
        self, symbol: str, action: str, score: float, strategy: str, reasoning: str = ""
    ):
        await self._db.execute(
            """INSERT INTO signals_log (symbol, action, score, strategy, reasoning, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (symbol, action, score, strategy, reasoning, datetime.utcnow().isoformat()),
        )
        await self._db.commit()

    async def log_api_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        success: bool = True,
        error: str | None = None,
    ):
        await self._db.execute(
            """INSERT INTO api_costs
            (model, input_tokens, output_tokens, cost_usd, timestamp, success, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                model,
                input_tokens,
                output_tokens,
                cost_usd,
                datetime.utcnow().isoformat(),
                1 if success else 0,
                error,
            ),
        )
        await self._db.commit()

    async def get_claude_status(self) -> dict:
        """Claude health from the most recent API call this session."""
        cursor = await self._db.execute(
            "SELECT success, error, timestamp FROM api_costs ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        if not row:
            return {"active": True, "error": None, "last_call": None}
        return {
            "active": bool(row["success"]),
            "error": row["error"],
            "last_call": row["timestamp"],
        }

    async def get_api_cost_today(self) -> float:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        cursor = await self._db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM api_costs WHERE timestamp LIKE ?",
            (f"{today}%",),
        )
        row = await cursor.fetchone()
        return row[0]

    async def save_whale_holdings(self, holdings: list[WhaleHolding]):
        now = datetime.utcnow().isoformat()
        for h in holdings:
            await self._db.execute(
                """INSERT OR REPLACE INTO whale_holdings
                (fund_name, cik, symbol, shares, value_usd, change_type,
                 change_pct, filing_date, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    h.fund_name,
                    h.cik,
                    h.symbol,
                    h.shares,
                    h.value_usd,
                    h.change_type,
                    h.change_pct,
                    h.filing_date,
                    now,
                ),
            )
        await self._db.commit()

    async def get_whale_consensus(self, symbol: str) -> dict:
        cursor = await self._db.execute(
            """SELECT fund_name, change_type, change_pct
            FROM whale_holdings WHERE symbol=?
            ORDER BY filing_date DESC""",
            (symbol,),
        )
        rows = await cursor.fetchall()
        result = {"buyers": [], "sellers": [], "holders": []}
        for r in rows:
            entry = {"fund": r["fund_name"], "change_pct": r["change_pct"]}
            if r["change_type"] in ("NEW", "INCREASED"):
                result["buyers"].append(entry)
            elif r["change_type"] in ("DECREASED", "SOLD"):
                result["sellers"].append(entry)
            else:
                result["holders"].append(entry)
        return result

    async def snapshot_portfolio(self, value: float, cash: float, positions: int):
        await self._db.execute(
            """INSERT INTO portfolio_snapshots (portfolio_value, cash, positions_count, timestamp)
            VALUES (?, ?, ?, ?)""",
            (value, cash, positions, datetime.utcnow().isoformat()),
        )
        await self._db.commit()

    async def get_daily_summary(self) -> DailySummary:
        today = datetime.utcnow().strftime("%Y-%m-%d")

        cursor = await self._db.execute(
            "SELECT * FROM trades WHERE timestamp LIKE ? ORDER BY timestamp DESC",
            (f"{today}%",),
        )
        today_trades = await cursor.fetchall()

        buys = sum(1 for t in today_trades if t["side"] == "BUY")
        sells = sum(1 for t in today_trades if t["side"] == "SELL")

        cursor = await self._db.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
        )
        snap = await cursor.fetchone()

        thirty_days_ago = (datetime.utcnow() - timedelta(days=30)).isoformat()
        cursor = await self._db.execute(
            "SELECT * FROM trades WHERE closed_at IS NOT NULL AND timestamp > ?",
            (thirty_days_ago,),
        )
        closed = await cursor.fetchall()
        wins = sum(1 for t in closed if t["pnl"] and t["pnl"] > 0)
        win_rate = (wins / len(closed) * 100) if closed else 0.0

        api_cost = await self.get_api_cost_today()

        return DailySummary(
            date=today,
            portfolio_value=snap["portfolio_value"] if snap else 0,
            daily_pnl=0,
            daily_pnl_pct=0,
            trades_count=len(today_trades),
            buys=buys,
            sells=sells,
            stop_losses=0,
            win_rate_30d=win_rate,
            open_positions=snap["positions_count"] if snap else 0,
            max_positions=15,
            api_cost_today=api_cost,
        )

    async def get_recent_signals(self, limit: int = 100) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM signals_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_equity_curve(self, days: int = 90) -> list[dict]:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        cursor = await self._db.execute(
            "SELECT * FROM portfolio_snapshots WHERE timestamp > ? ORDER BY timestamp ASC",
            (since,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
