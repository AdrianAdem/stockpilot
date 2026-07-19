import time

import httpx
import pandas as pd
import structlog

logger = structlog.get_logger()

_cached_universe: list[str] = []
_last_update: float = 0
_CACHE_TTL = 7 * 24 * 3600  # 1 week
_name_to_ticker: dict[str, str] = {}  # normalized company name -> ticker
_ticker_to_sector: dict[str, str] = {}  # ticker -> GICS sector


def get_sector_for(ticker: str) -> str:
    return _ticker_to_sector.get(ticker, "Unknown")


def _normalize_name(name: str) -> str:
    """Strip corporate suffixes/punctuation so 13F issuer names match S&P names."""
    n = name.upper()
    for ch in [".", ",", "&", "'", "-", "/"]:
        n = n.replace(ch, " ")
    drop = {
        "INC",
        "CORP",
        "CORPORATION",
        "CO",
        "COMPANY",
        "LTD",
        "LLC",
        "PLC",
        "THE",
        "CLASS",
        "CL",
        "A",
        "B",
        "C",
        "COM",
        "HLDGS",
        "HOLDINGS",
        "GROUP",
        "GRP",
        "INTERNATIONAL",
        "INTL",
        "INDS",
        "INDUSTRIES",
    }
    tokens = [t for t in n.split() if t and t not in drop]
    return " ".join(tokens)


async def fetch_sp500() -> list[str]:
    global _name_to_ticker
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    # Wikipedia 403s requests without a real User-Agent.
    headers = {"User-Agent": "Mozilla/5.0 (stockpilot data fetch)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        resp = await client.get(url, timeout=30)
        resp.raise_for_status()
    tables = pd.read_html(resp.text)
    df = tables[0]
    # Keep dot notation (BRK.B, BF.B) — Alpaca's API expects dots, not dashes.
    symbols = [str(s).strip() for s in df["Symbol"].tolist()]
    # Build name->ticker and ticker->sector maps from the Wikipedia table.
    sector_col = next((c for c in df.columns if "GICS Sector" in str(c)), None)
    for _, r in df.iterrows():
        tic = str(r["Symbol"]).strip()
        if "Security" in df.columns:
            norm = _normalize_name(str(r["Security"]))
            if norm:
                _name_to_ticker[norm] = tic
        if sector_col:
            _ticker_to_sector[tic] = str(r[sector_col]).strip()
    logger.info(
        "sp500_fetched",
        count=len(symbols),
        name_map=len(_name_to_ticker),
        sectors=len(_ticker_to_sector),
    )
    return symbols


def resolve_ticker(issuer_name: str) -> str | None:
    """Map a 13F nameOfIssuer to a known ticker, or None if not in universe."""
    return _name_to_ticker.get(_normalize_name(issuer_name))


async def get_universe(extra_watchlist: list[str] | None = None) -> list[str]:
    global _cached_universe, _last_update

    if _cached_universe and (time.time() - _last_update) < _CACHE_TTL:
        return _cached_universe

    try:
        symbols = await fetch_sp500()
    except Exception as e:
        logger.error("sp500_fetch_failed", error=str(e))
        symbols = []

    if extra_watchlist:
        for s in extra_watchlist:
            if s not in symbols:
                symbols.append(s)

    _cached_universe = sorted(set(symbols))
    _last_update = time.time()
    logger.info("universe_updated", count=len(_cached_universe))
    return _cached_universe
