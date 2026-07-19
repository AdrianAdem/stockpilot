from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from storage.db import Database

app = FastAPI(title="StockPilot Dashboard")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

db: Database | None = None


def create_app(database: Database) -> FastAPI:
    global db
    db = database
    return app


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    equity_curve = await db.get_equity_curve(90)
    open_trades = await db.get_open_trades()
    summary = await db.get_daily_summary()
    claude_status = await db.get_claude_status()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "equity_curve": equity_curve,
            "open_trades": open_trades,
            "summary": summary,
            "claude_status": claude_status,
        },
    )


@app.get("/positions", response_class=HTMLResponse)
async def positions(request: Request):
    trades = await db.get_open_trades()
    return templates.TemplateResponse(
        request,
        "positions.html",
        {
            "trades": trades,
        },
    )


@app.get("/signals", response_class=HTMLResponse)
async def signals(request: Request):
    recent = await db.get_recent_signals(100)
    return templates.TemplateResponse(
        request,
        "signals.html",
        {
            "signals": recent,
        },
    )


@app.get("/whales", response_class=HTMLResponse)
async def whales(request: Request):
    # Get whale data from DB
    cursor = await db._db.execute("SELECT * FROM whale_holdings ORDER BY fetched_at DESC LIMIT 200")
    rows = await cursor.fetchall()
    holdings = [dict(r) for r in rows]
    return templates.TemplateResponse(
        request,
        "whales.html",
        {
            "holdings": holdings,
        },
    )


@app.get("/costs", response_class=HTMLResponse)
async def costs(request: Request):
    cursor = await db._db.execute("SELECT * FROM api_costs ORDER BY timestamp DESC LIMIT 100")
    rows = await cursor.fetchall()
    cost_data = [dict(r) for r in rows]
    total_today = await db.get_api_cost_today()
    return templates.TemplateResponse(
        request,
        "costs.html",
        {
            "costs": cost_data,
            "total_today": total_today,
        },
    )


@app.get("/backtest", response_class=HTMLResponse)
async def backtest(request: Request):
    reports_dir = Path(__file__).parent.parent / "reports"
    reports = []
    if reports_dir.exists():
        for f in sorted(reports_dir.glob("*.html"), reverse=True):
            reports.append({"name": f.stem, "path": f.name, "size": f.stat().st_size})
    return templates.TemplateResponse(
        request,
        "backtest.html",
        {
            "reports": reports,
        },
    )


@app.get("/backtest/{filename}", response_class=HTMLResponse)
async def backtest_report(filename: str):
    report_path = Path(__file__).parent.parent / "reports" / filename
    if not report_path.exists() or not filename.endswith(".html"):
        return HTMLResponse("Report not found", status_code=404)
    return HTMLResponse(report_path.read_text())


@app.get("/logs", response_class=HTMLResponse)
async def logs(request: Request):
    trades = await db.get_recent_trades(30)
    signals = await db.get_recent_signals(100)
    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "trades": trades,
            "signals": signals,
        },
    )


@app.get("/api/equity-curve")
async def api_equity_curve():
    data = await db.get_equity_curve(90)
    return data


@app.get("/api/summary")
async def api_summary():
    summary = await db.get_daily_summary()
    return summary.model_dump()


async def start_dashboard(database: Database, host: str = "0.0.0.0", port: int = 8000):
    import uvicorn

    global db
    db = database
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
