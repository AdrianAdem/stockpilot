import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx
import structlog

from storage.models import WhaleHolding

logger = structlog.get_logger()

TRACKED_FUNDS = {
    "0001067983": "Berkshire Hathaway",
    "0001350694": "Bridgewater Associates",
    "0001029160": "Soros Fund Management",
    "0001037389": "Renaissance Technologies",
    "0001423053": "Citadel Advisors",
    "0001336528": "Pershing Square",
    "0001040273": "Third Point",
    "0001656456": "Appaloosa Management",
}

# CUSIP to ticker mapping is imperfect; we maintain a cache
_cusip_cache: dict[str, str] = {}


class SECFilingsClient:
    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self._client = httpx.AsyncClient(
            timeout=30,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip"},
        )
        self._filings_cache: dict[str, list[dict]] = {}
        self._last_fetch: dict[str, datetime] = {}

    async def close(self):
        await self._client.aclose()

    async def _sec_request(self, url: str) -> httpx.Response:
        # SEC rate limit: 10 req/sec
        await asyncio.sleep(0.15)
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp

    async def get_filing_index(self, cik: str) -> list[dict]:
        url = f"https://efts.sec.gov/LATEST/search-index?q=%2213F%22&dateRange=custom&startdt=2024-01-01&forms=13F-HR&entityName={cik}"
        try:
            # Use EDGAR filing API
            submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            resp = await self._sec_request(submissions_url)
            data = resp.json()
            filings = data.get("filings", {}).get("recent", {})
            forms = filings.get("form", [])
            accessions = filings.get("accessionNumber", [])
            dates = filings.get("filingDate", [])

            results = []
            for form, accession, date in zip(forms, accessions, dates):
                if form == "13F-HR":
                    results.append({
                        "accession": accession.replace("-", ""),
                        "accession_raw": accession,
                        "date": date,
                    })
            return results[:4]  # last 4 filings
        except Exception as e:
            logger.error("sec_filing_index_error", cik=cik, error=str(e))
            return []

    async def fetch_13f_holdings(self, cik: str, accession: str) -> list[dict]:
        # Try to fetch the information table XML
        acc_formatted = accession[:10] + "-" + accession[10:12] + "-" + accession[12:]
        base = f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{accession}"

        try:
            # Get filing index
            index_url = f"{base}/index.json"
            resp = await self._sec_request(index_url)
            index_data = resp.json()

            xml_file = None
            for item in index_data.get("directory", {}).get("item", []):
                name = item.get("name", "").lower()
                if "infotable" in name or "information" in name:
                    xml_file = item["name"]
                    break
                if name.endswith(".xml") and "primary" not in name:
                    xml_file = item["name"]

            if not xml_file:
                logger.warning("no_xml_found", cik=cik, accession=accession)
                return []

            xml_url = f"{base}/{xml_file}"
            resp = await self._sec_request(xml_url)
            return self._parse_13f_xml(resp.text)

        except Exception as e:
            logger.error("13f_fetch_error", cik=cik, error=str(e))
            return []

    def _parse_13f_xml(self, xml_text: str) -> list[dict]:
        holdings = []
        try:
            root = ET.fromstring(xml_text)
            # Handle namespace
            ns = ""
            for elem in root.iter():
                if "}" in elem.tag:
                    ns = elem.tag.split("}")[0] + "}"
                    break

            for info in root.iter(f"{ns}infoTable"):
                name_el = info.find(f"{ns}nameOfIssuer")
                cusip_el = info.find(f"{ns}cusip")
                value_el = info.find(f"{ns}value")
                shares_el = info.find(f".//{ns}sshPrnamt")

                if name_el is not None and cusip_el is not None:
                    holdings.append({
                        "name": name_el.text or "",
                        "cusip": cusip_el.text or "",
                        "value": int(value_el.text or "0") * 1000,  # reported in thousands
                        "shares": int(shares_el.text or "0") if shares_el is not None else 0,
                    })
        except ET.ParseError as e:
            logger.error("xml_parse_error", error=str(e))
        return holdings

    async def fetch_latest_13f(self, cik: str) -> list[dict]:
        cache_key = cik
        if cache_key in self._last_fetch:
            elapsed = (datetime.utcnow() - self._last_fetch[cache_key]).total_seconds()
            if elapsed < 86400 and cache_key in self._filings_cache:
                return self._filings_cache[cache_key]

        filings = await self.get_filing_index(cik)
        if not filings:
            return []

        latest = filings[0]
        holdings = await self.fetch_13f_holdings(cik, latest["accession"])
        self._filings_cache[cache_key] = holdings
        self._last_fetch[cache_key] = datetime.utcnow()
        logger.info("13f_fetched", cik=cik, fund=TRACKED_FUNDS.get(cik, ""),
                     holdings_count=len(holdings), date=latest["date"])
        return holdings

    async def compare_filings(self, cik: str) -> list[dict]:
        filings = await self.get_filing_index(cik)
        if len(filings) < 2:
            return []

        current = await self.fetch_13f_holdings(cik, filings[0]["accession"])
        previous = await self.fetch_13f_holdings(cik, filings[1]["accession"])

        current_map = {h["cusip"]: h for h in current}
        previous_map = {h["cusip"]: h for h in previous}

        changes = []
        for cusip, h in current_map.items():
            if cusip not in previous_map:
                changes.append({**h, "change_type": "NEW", "change_pct": None})
            else:
                prev = previous_map[cusip]
                if prev["shares"] > 0:
                    pct = (h["shares"] - prev["shares"]) / prev["shares"] * 100
                else:
                    pct = 100.0
                if pct > 5:
                    changes.append({**h, "change_type": "INCREASED", "change_pct": round(pct, 1)})
                elif pct < -5:
                    changes.append({**h, "change_type": "DECREASED", "change_pct": round(pct, 1)})
                else:
                    changes.append({**h, "change_type": "UNCHANGED", "change_pct": round(pct, 1)})

        for cusip in previous_map:
            if cusip not in current_map:
                changes.append({**previous_map[cusip], "change_type": "SOLD", "change_pct": -100.0})

        return changes

    async def get_new_positions(self) -> list[dict]:
        all_new = []
        for cik, name in TRACKED_FUNDS.items():
            try:
                changes = await self.compare_filings(cik)
                for c in changes:
                    if c["change_type"] == "NEW":
                        all_new.append({**c, "fund": name, "cik": cik})
            except Exception as e:
                logger.error("compare_error", fund=name, error=str(e))
        return all_new

    async def get_increased_positions(self) -> list[dict]:
        all_increased = []
        for cik, name in TRACKED_FUNDS.items():
            try:
                changes = await self.compare_filings(cik)
                for c in changes:
                    if c["change_type"] == "INCREASED":
                        all_increased.append({**c, "fund": name, "cik": cik})
            except Exception as e:
                logger.error("compare_error", fund=name, error=str(e))
        return all_increased

    async def update_filings(self):
        logger.info("updating_all_13f_filings")
        for cik, name in TRACKED_FUNDS.items():
            try:
                await self.fetch_latest_13f(cik)
            except Exception as e:
                logger.error("filing_update_error", fund=name, error=str(e))
            await asyncio.sleep(1)  # rate limit
