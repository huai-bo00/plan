from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
import csv
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import requests
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
from .crawling import policy_crawler

log = logging.getLogger("mining_demo")
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _configured_path(value: str | Path, default: Path) -> Path:
    path = Path(value) if value else default
    return path if path.is_absolute() else ROOT / path


@dataclass
class Record:
    id: str
    category: str
    title: str
    content: str
    published_at: str
    source: str
    url: str
    commodity: str | None = None
    value: float | None = None
    unit: str | None = None
    data_quality: str = "live"


class Provider(Protocol):
    name: str
    def fetch(self) -> list[Record]: ...


class RSSProvider:
    def __init__(self, name: str, url: str, category: str):
        self.name, self.url, self.category = name, url, category

    def fetch(self) -> list[Record]:
        response = requests.get(self.url, timeout=float(os.getenv("REQUEST_TIMEOUT", "12")), headers={"User-Agent": "MiningIntelDemo/1.0"})
        response.raise_for_status()
        root = ET.fromstring(response.content)
        records = []
        for item in root.findall(".//item")[:100]:
            title = _text(item.find("title"))
            link = _text(item.find("link"))
            description = _text(item.find("description"))
            pub = _text(item.find("pubDate"))
            if not title or not link:
                continue
            try:
                published = datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S").replace(tzinfo=timezone.utc).date().isoformat()
            except (ValueError, TypeError):
                published = date.today().isoformat()
            clean = re.sub(r"<[^>]+>", " ", description)
            clean = re.sub(r"\s+", " ", clean).strip()
            records.append(Record(_id(self.category, link), self.category, title, clean[:1800], published, self.name, link))
        return records


class YahooChartProvider:
    """Public Yahoo chart endpoint adapter; symbols can be configured by operator."""
    LABELS = {"HG=F": ("Copper", "USD/lb"), "GC=F": ("Gold", "USD/oz"), "SI=F": ("Silver", "USD/oz"), "ALI=F": ("Aluminum", "USD/tonne"), "NI=F": ("Nickel", "USD/tonne")}
    def __init__(self, symbols: list[str] | None = None):
        self.symbols = symbols or [s.strip() for s in os.getenv("PRICE_SYMBOLS", "HG=F,GC=F,SI=F").split(",") if s.strip()]
        self.name = "Yahoo Finance public chart data"

    def fetch(self) -> list[Record]:
        out: list[Record] = []
        now = int(time.time())
        for symbol in self.symbols:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='=')}?period1={now-45*86400}&period2={now}&interval=1d"
            response = requests.get(url, timeout=float(os.getenv("REQUEST_TIMEOUT", "12")), headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            result = response.json()["chart"]["result"][0]
            name, unit = self.LABELS.get(symbol, (symbol, result.get("meta", {}).get("currency", "quote units")))
            closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            for stamp, value in zip(result.get("timestamp", []), closes):
                if value is None:
                    continue
                day = datetime.fromtimestamp(stamp, tz=timezone.utc).date().isoformat()
                out.append(Record(_id("price", f"{symbol}:{day}"), "price", f"{name} closing price", f"Daily close for {name} futures.", day, f"{self.name} ({symbol})", f"https://finance.yahoo.com/quote/{quote(symbol, safe='=')}/", name, round(float(value), 4), unit))
        return out


class OfficialPolicyProvider:
    """Fetch a small set of stable official policy source pages for durable context."""
    category = "policy"
    name = "Official critical minerals policy pages"
    PAGES = [
        ("Australian Critical Minerals Strategy", "https://www.industry.gov.au/mining-oil-and-gas/minerals/critical-minerals", "2023-06-20", "Australia Department of Industry, Science and Resources"),
        ("Rare Earth Management Regulation", "https://www.gov.cn/zhengce/content/202406/content_6960153.htm", "2024-06-29", "State Council of the People's Republic of China"),
    ]

    def __init__(self, crawler=None):
        self.crawler = crawler or policy_crawler()

    def fetch(self) -> list[Record]:
        records = []
        for title, url, published, publisher in self.PAGES:
            result = self.crawler.crawl(url)
            log.info("policy_source_done url=%s engine=%s success=%s visited=%d chars=%d", url, result.engine, result.success, len(result.visited_urls), len(result.text))
            if not result.success:
                log.warning("policy_source_unavailable url=%s error=%s", url, result.error)
                continue
            records.append(Record(_id("policy", url), "policy", title, result.text[:5000], published, publisher, url))
        if not records:
            raise RuntimeError("all official policy pages failed; provider cache/sample fallback will be used")
        return records


class OfficialCSVPriceProvider:
    """Import official/vendor CSV exports without scraping access-controlled feeds."""
    category = "price"
    def __init__(self, name: str, csv_path: Path, source_url: str):
        self.name, self.csv_path, self.source_url = name, csv_path, source_url

    def fetch(self) -> list[Record]:
        if not self.csv_path.exists():
            return []
        out = []
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as file:
            for row in csv.DictReader(file):
                day = date.fromisoformat(row["date"][:10]).isoformat()
                commodity = row["commodity"].strip()
                value = float(row["value"])
                unit = row["unit"].strip()
                source = (row.get("source") or self.name).strip()
                url = (row.get("url") or self.source_url).strip()
                out.append(Record(_id("price", f"{self.name}:{commodity}:{day}"), "price", f"{commodity} reference price", f"Official/vendor CSV market data for {commodity}.", day, source, url, commodity, value, unit))
        return out


def default_providers() -> list[Provider]:
    return [
        RSSProvider("Mining.com RSS", os.getenv("NEWS_RSS_URL", "https://www.mining.com/feed/"), "news"),
        RSSProvider("Google News mining industry feed", os.getenv("MINING_NEWS_RSS_URL", "https://news.google.com/rss/search?q=mining+industry+critical+minerals&hl=en-US&gl=US&ceid=US:en"), "news"),
        RSSProvider("Google News critical minerals feed", os.getenv("POLICY_RSS_URL", "https://news.google.com/rss/search?q=critical+minerals+policy&hl=en-US&gl=US&ceid=US:en"), "policy"),
        OfficialPolicyProvider(),
        YahooChartProvider(),
        OfficialCSVPriceProvider("LME official/vendor CSV", ROOT / "storage/imports/lme.csv", "https://datalicensing.lme.com/agr/distribution"),
        OfficialCSVPriceProvider("SHFE official/vendor CSV", ROOT / "storage/imports/shfe.csv", "https://www.shfe.com.cn/reports/tradedata/datadownload/"),
    ]


def ingest(providers: list[Provider] | None = None, *, data_path: Path | None = None, cache_path: Path | None = None, trace_id: str | None = None) -> dict[str, Any]:
    """Fetch providers independently; use last cached provider data on failures."""
    trace_id = trace_id or f"ing_{uuid.uuid4().hex[:12]}"
    pipeline_start = time.perf_counter()
    log.info("pipeline_start trace_id=%s", trace_id)
    data_path = data_path or _configured_path(os.getenv("APP_DATA_PATH", ROOT / "storage/data.json"), ROOT / "storage/data.json")
    cache_path = cache_path or _configured_path(os.getenv("APP_CACHE_PATH", ROOT / "storage/cache"), ROOT / "storage/cache")
    cache_path.mkdir(parents=True, exist_ok=True)
    records: list[Record] = []
    failed_categories: set[str] = set()
    statuses = []
    for provider in providers or default_providers():
        cache_file = cache_path / f"{re.sub(r'[^a-zA-Z0-9_-]+', '_', provider.name.lower())}.json"
        started = time.perf_counter()
        try:
            fetched = provider.fetch()
            _write_json_atomic(cache_file, [asdict(r) for r in fetched])
            records.extend(fetched)
            duration = round((time.perf_counter()-started)*1000, 1)
            statuses.append({"provider": provider.name, "status": "ok", "count": len(fetched), "duration_ms": duration})
            log.info("pipeline_stage_done trace_id=%s stage=fetch provider=%s count=%d duration_ms=%.1f status=ok", trace_id, provider.name, len(fetched), duration)
        except Exception as exc:
            log.warning("provider_failed trace_id=%s name=%s error=%s", trace_id, provider.name, exc)
            fallback = _read_records(cache_file) if cache_file.exists() else []
            records.extend(fallback)
            if not fallback:
                failed_categories.add({"news": "news", "policy": "policy"}.get(getattr(provider, "category", ""), "price"))
            duration = round((time.perf_counter()-started)*1000, 1)
            statuses.append({"provider": provider.name, "status": "cached" if fallback else "failed", "count": len(fallback), "error": str(exc)[:300], "duration_ms": duration})
            log.warning("pipeline_stage_done trace_id=%s stage=fetch provider=%s count=%d duration_ms=%.1f status=%s", trace_id, provider.name, len(fallback), duration, statuses[-1]["status"])
    # Keep a deterministic offline path for source classes unavailable on this run.
    demo_records = _read_records(ROOT / "storage" / "demo_data.json")
    live_categories = {r.category for r in records if r.data_quality == "live"}
    live_price_commodities = {r.commodity.lower() for r in records if r.category == "price" and r.commodity and r.data_quality == "live"}
    for demo in demo_records:
        if (demo.category in {"news", "policy"} and demo.category in failed_categories and demo.category not in live_categories) or (
            demo.category == "price" and demo.commodity and not any(demo.commodity.lower() in commodity or commodity in demo.commodity.lower() for commodity in live_price_commodities)
        ):
            records.append(demo)
    normalize_start = time.perf_counter()
    records = normalize_dedupe(records)
    normalize_ms = round((time.perf_counter()-normalize_start)*1000, 1)
    log.info("pipeline_stage_done trace_id=%s stage=normalize_dedupe count=%d duration_ms=%.1f status=ok", trace_id, len(records), normalize_ms)
    _write_json_atomic(data_path, [asdict(r) for r in records])
    total_ms = round((time.perf_counter()-pipeline_start)*1000, 1)
    log.info("pipeline_done trace_id=%s count=%d duration_ms=%.1f status=ok", trace_id, len(records), total_ms)
    return {"records": len(records), "providers": statuses, "data_path": str(data_path), "trace_id": trace_id, "timings_ms": {"normalize_dedupe": normalize_ms, "total": total_ms}}


def load_records(data_path: Path | None = None) -> list[Record]:
    data_path = data_path or _configured_path(os.getenv("APP_DATA_PATH", ROOT / "storage/data.json"), ROOT / "storage/data.json")
    if not data_path.exists():
        sample = ROOT / "storage/demo_data.json"
        records = _read_records(sample)
    else:
        records = _read_records(data_path)
    illustrative = [r for r in records if r.data_quality != "live"]
    if illustrative:
        latest = max(date.fromisoformat(r.published_at) for r in illustrative)
        offset = (date.today() - latest).days
        if offset:
            for r in illustrative:
                r.published_at = (date.fromisoformat(r.published_at) + timedelta(days=offset)).isoformat()
    return normalize_dedupe(records)


def normalize_dedupe(records: list[Record]) -> list[Record]:
    out, seen = [], set()
    for r in records:
        title = re.sub(r"\s+", " ", r.title).strip()
        content = re.sub(r"\s+", " ", r.content).strip()
        if r.category == "price":
            key = f"price:{(r.commodity or '').lower()}:{r.published_at}:{(r.unit or '').lower()}:{(r.source or '').lower()}"
        else:
            key = r.url.strip().rstrip("/") if r.url else f"{r.category}:{title.lower()}:{r.published_at}"
        if not title or key in seen:
            continue
        seen.add(key)
        try:
            published = date.fromisoformat(r.published_at[:10]).isoformat()
        except (ValueError, TypeError):
            continue
        val = round(float(r.value), 6) if r.value is not None else None
        out.append(Record(r.id or _id(r.category, key), r.category, title, content, published, r.source, r.url, r.commodity, val, r.unit, r.data_quality))
    return sorted(out, key=lambda r: (r.published_at, r.category), reverse=True)


def _read_records(path: Path) -> list[Record]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return normalize_dedupe([Record(**x) for x in raw])


def _write_json_atomic(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _text(node: Any) -> str:
    return "" if node is None or node.text is None else node.text.strip()


def _id(category: str, value: str) -> str:
    return f"{category}_{uuid.uuid5(uuid.NAMESPACE_URL, value).hex[:12]}"
