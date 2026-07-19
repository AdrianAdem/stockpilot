import asyncio
from datetime import datetime, timedelta

import feedparser
import httpx
import structlog

logger = structlog.get_logger()

# Reuters killed its public RSS (feeds.reuters.com no longer resolves).
RSS_FEEDS = [
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",
]


class NewsAggregator:
    def __init__(self):
        self._cache: dict[str, list[dict]] = {}
        self._last_update: datetime | None = None
        self._client = httpx.AsyncClient(timeout=15)

    async def close(self):
        await self._client.aclose()

    async def fetch_rss(self) -> list[dict]:
        articles = []
        for feed_url in RSS_FEEDS:
            try:
                resp = await self._client.get(feed_url)
                feed = feedparser.parse(resp.text)
                for entry in feed.entries[:20]:
                    articles.append({
                        "title": entry.get("title", ""),
                        "summary": entry.get("summary", "")[:300],
                        "link": entry.get("link", ""),
                        "published": entry.get("published", ""),
                        "source": feed.feed.get("title", feed_url),
                    })
            except Exception as e:
                logger.warning("rss_fetch_error", url=feed_url, error=str(e))
        return articles

    async def fetch_alpaca_news(self, alpaca_client, symbols: list[str] | None = None) -> list[dict]:
        try:
            news = await alpaca_client.get_news(symbols=symbols, limit=50)
            return [
                {
                    "title": n.get("headline", ""),
                    "summary": n.get("summary", "")[:300],
                    "symbols": n.get("symbols", []),
                    "source": n.get("source", ""),
                    "timestamp": n.get("created_at", ""),
                }
                for n in news
            ]
        except Exception as e:
            logger.warning("alpaca_news_error", error=str(e))
            return []

    def get_news_for_symbol(self, symbol: str) -> list[dict]:
        return self._cache.get(symbol, [])

    async def update(self, alpaca_client=None, symbols: list[str] | None = None):
        articles = await self.fetch_rss()

        if alpaca_client and symbols:
            alpaca_news = await self.fetch_alpaca_news(alpaca_client, symbols)
            articles.extend(alpaca_news)

        # Index by symbol mentions
        self._cache.clear()
        if symbols:
            for symbol in symbols:
                matching = []
                for a in articles:
                    title = a.get("title", "").upper()
                    summary = a.get("summary", "").upper()
                    syms = [s.upper() for s in a.get("symbols", [])]
                    if symbol.upper() in title or symbol.upper() in summary or symbol.upper() in syms:
                        matching.append(a)
                if matching:
                    self._cache[symbol] = matching[:10]

        self._last_update = datetime.utcnow()
        logger.info("news_updated", articles=len(articles), symbols_with_news=len(self._cache))

    def get_market_sentiment(self) -> str:
        all_articles = []
        for articles in self._cache.values():
            all_articles.extend(articles)
        if not all_articles:
            return "neutral"

        negative_words = ["crash", "plunge", "fear", "recession", "crisis", "selloff", "decline", "bear"]
        positive_words = ["rally", "surge", "boom", "bull", "growth", "record", "gain", "optimism"]

        text = " ".join(a.get("title", "") + " " + a.get("summary", "") for a in all_articles).lower()
        neg = sum(text.count(w) for w in negative_words)
        pos = sum(text.count(w) for w in positive_words)

        if pos > neg * 1.5:
            return "bullish"
        elif neg > pos * 1.5:
            return "bearish"
        return "neutral"
