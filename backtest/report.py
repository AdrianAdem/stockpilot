import base64
import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from backtest.engine import BacktestResult


def generate_html_report(result: BacktestResult, strategy_name: str = "Combined") -> str:
    equity_chart = _plot_equity_curve(result.equity_curve)
    drawdown_chart = _plot_drawdown(result.drawdown_curve)
    _plot_monthly_returns(result)

    html = f"""<!DOCTYPE html>
<html>
<head>
<title>StockPilot Backtest Report - {strategy_name}</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background: #0f172a; color: #e2e8f0; }}
h1 {{ color: #38bdf8; }}
h2 {{ color: #7dd3fc; border-bottom: 1px solid #334155; padding-bottom: 8px; }}
.metrics {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin: 20px 0; }}
.metric {{ background: #1e293b; border-radius: 8px; padding: 16px; text-align: center; }}
.metric-value {{ font-size: 24px; font-weight: bold; color: #38bdf8; }}
.metric-label {{ font-size: 12px; color: #94a3b8; margin-top: 4px; }}
.positive {{ color: #4ade80; }}
.negative {{ color: #f87171; }}
img {{ max-width: 100%; border-radius: 8px; margin: 10px 0; }}
table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #334155; }}
th {{ background: #1e293b; }}
</style>
</head>
<body>
<h1>StockPilot Backtest Report</h1>
<p>Strategy: <b>{strategy_name}</b> | Period: {result.start_date} to {result.end_date}</p>

<div class="metrics">
<div class="metric"><div class="metric-value {"positive" if result.total_return_pct > 0 else "negative"}">{result.total_return_pct:+.1f}%</div><div class="metric-label">Total Return</div></div>
<div class="metric"><div class="metric-value">{result.cagr:.1f}%</div><div class="metric-label">CAGR</div></div>
<div class="metric"><div class="metric-value">{result.sharpe_ratio:.2f}</div><div class="metric-label">Sharpe Ratio</div></div>
<div class="metric"><div class="metric-value negative">{result.max_drawdown:.1f}%</div><div class="metric-label">Max Drawdown</div></div>
<div class="metric"><div class="metric-value">{result.win_rate:.0f}%</div><div class="metric-label">Win Rate</div></div>
<div class="metric"><div class="metric-value">{result.profit_factor:.2f}</div><div class="metric-label">Profit Factor</div></div>
<div class="metric"><div class="metric-value">{result.total_trades}</div><div class="metric-label">Total Trades</div></div>
<div class="metric"><div class="metric-value">${result.final_capital:,.0f}</div><div class="metric-label">Final Capital</div></div>
</div>

<h2>Equity Curve</h2>
<img src="data:image/png;base64,{equity_chart}">

<h2>Drawdown</h2>
<img src="data:image/png;base64,{drawdown_chart}">

<h2>Trade Statistics</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
<tr><td>Winning Trades</td><td>{result.winning_trades}</td></tr>
<tr><td>Losing Trades</td><td>{result.losing_trades}</td></tr>
<tr><td>Avg Win</td><td class="positive">${result.avg_win:,.2f}</td></tr>
<tr><td>Avg Loss</td><td class="negative">${result.avg_loss:,.2f}</td></tr>
<tr><td>Initial Capital</td><td>${result.initial_capital:,.0f}</td></tr>
<tr><td>Final Capital</td><td>${result.final_capital:,.0f}</td></tr>
</table>

<h2>Recent Trades</h2>
<table>
<tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Date</th></tr>
{"".join(_trade_row(t) for t in result.trades[-20:])}
</table>
</body></html>"""
    return html


def _trade_row(t) -> str:
    pnl_class = "positive" if t.pnl > 0 else "negative"
    return f"""<tr>
<td>{t.symbol}</td><td>{t.side}</td>
<td>${t.entry_price:.2f}</td><td>${t.exit_price:.2f}</td>
<td class="{pnl_class}">${t.pnl:,.2f}</td><td>{t.exit_date}</td></tr>"""


def _plot_equity_curve(curve: list[float]) -> str:
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(curve, color="#38bdf8", linewidth=1.5)
    ax.fill_between(range(len(curve)), curve, alpha=0.1, color="#38bdf8")
    ax.set_facecolor("#0f172a")
    fig.patch.set_facecolor("#0f172a")
    ax.tick_params(colors="#94a3b8")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#334155")
    ax.spines["left"].set_color("#334155")
    ax.set_ylabel("Portfolio Value ($)", color="#94a3b8")
    ax.grid(True, alpha=0.1)
    return _fig_to_base64(fig)


def _plot_drawdown(curve: list[float]) -> str:
    fig, ax = plt.subplots(figsize=(12, 3))
    dd_pct = [d * 100 for d in curve]
    ax.fill_between(range(len(dd_pct)), dd_pct, alpha=0.5, color="#f87171")
    ax.plot(dd_pct, color="#f87171", linewidth=1)
    ax.set_facecolor("#0f172a")
    fig.patch.set_facecolor("#0f172a")
    ax.tick_params(colors="#94a3b8")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#334155")
    ax.spines["left"].set_color("#334155")
    ax.set_ylabel("Drawdown (%)", color="#94a3b8")
    ax.grid(True, alpha=0.1)
    return _fig_to_base64(fig)


def _plot_monthly_returns(result: BacktestResult) -> str:
    # Simplified: just return empty for now
    return ""


def _fig_to_base64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def save_report(html: str, filename: str = "backtest_report.html"):
    path = Path(__file__).parent.parent / "reports" / filename
    path.parent.mkdir(exist_ok=True)
    path.write_text(html)
    return str(path)
