"""Small, bounded official-page crawler with an optional Crawl4AI fallback."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("mining_demo")
SKIP_SUFFIXES = (".pdf", ".jpg", ".jpeg", ".png", ".zip", ".doc", ".docx", ".xls", ".xlsx")
POLICY_TERMS = ("policy", "strategy", "critical-mineral", "critical_mineral", "regulation", "minerals", "政策", "战略", "条例", "矿产")


@dataclass
class CrawlResult:
    success: bool
    url: str
    text: str = ""
    visited_urls: list[str] = field(default_factory=list)
    error: str = ""
    engine: str = "requests"


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for node in list(soup.select("script,style,noscript,nav,footer,header,aside,form,svg,button")):
        node.decompose()
    for node in list(soup.find_all(True)):
        labels = " ".join([str(node.get("id") or ""), " ".join(node.get("class") or [])]).lower()
        if any(word in labels for word in ("cookie", "breadcrumb", "share", "social", "related", "comment", "footer", "header")):
            node.decompose()
    roots = soup.select("main, article, [role=main]")
    text = max((node.get_text(" ", strip=True) for node in roots), key=len, default="")
    if len(text) < 80:
        text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?:Accept all cookies|Cookie settings|Privacy policy)\b", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


class RequestsPolicyCrawler:
    def __init__(self, *, max_pages: int = 4, max_depth: int = 1, timeout: float = 8, min_text_length: int = 120):
        self.max_pages, self.max_depth = max_pages, max_depth
        self.timeout, self.min_text_length = timeout, min_text_length

    def crawl(self, url: str) -> CrawlResult:
        start = url.strip()
        parsed = urlparse(start)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return CrawlResult(False, url, error="invalid URL")
        origin = (parsed.scheme, parsed.netloc.lower())
        queue = deque([(start, 0)])
        queued, visited, chunks = {start}, [], []
        started = time.perf_counter()
        while queue and len(visited) < self.max_pages:
            page_url, depth = queue.popleft()
            try:
                response = requests.get(page_url, timeout=self.timeout, headers={
                    "User-Agent": "MiningIntelDemo/1.0 (+official policy page reader)",
                    "Accept": "text/html,application/xhtml+xml",
                })
                response.raise_for_status()
                if "html" not in response.headers.get("Content-Type", "text/html").lower():
                    continue
                html = response.text
                visited.append(page_url)
                text = clean_html(html)
                if len(text) >= self.min_text_length:
                    chunks.append(text)
                if depth >= self.max_depth:
                    continue
                soup = BeautifulSoup(html, "html.parser")
                candidates = []
                for anchor in soup.find_all("a", href=True):
                    candidate = urldefrag(urljoin(page_url, anchor["href"]))[0]
                    p = urlparse(candidate)
                    label = f"{anchor.get_text(' ', strip=True)} {candidate}".lower()
                    if (p.scheme, p.netloc.lower()) != origin or p.path.lower().endswith(SKIP_SUFFIXES):
                        continue
                    if candidate not in queued and any(term in label for term in POLICY_TERMS):
                        candidates.append(candidate)
                for candidate in candidates:
                    queued.add(candidate)
                    queue.append((candidate, depth + 1))
            except Exception as exc:
                log.info("policy_crawl_page_failed url=%s error=%s", page_url, exc)
                if not visited:
                    return CrawlResult(False, start, visited_urls=visited, error=str(exc))
        text = re.sub(r"\s+", " ", " ".join(chunks)).strip()
        duration = (time.perf_counter() - started) * 1000
        log.info("policy_crawl_done engine=requests url=%s visited=%d chars=%d duration_ms=%.1f", start, len(visited), len(text), duration)
        if len(text) < self.min_text_length:
            return CrawlResult(False, start, text, visited, "insufficient extracted text")
        return CrawlResult(True, start, text[:12000], visited)


class Crawl4AIPolicyCrawler:
    """Lazy optional adapter; base installation does not require browser dependencies."""
    def __init__(self, *, max_pages: int = 4, max_depth: int = 1, timeout: float = 8, min_text_length: int = 120):
        self.max_pages, self.max_depth = max_pages, max_depth
        self.timeout, self.min_text_length = timeout, min_text_length

    def crawl(self, url: str) -> CrawlResult:
        try:
            raw = self._run(self._crawl_async(url))
            results = raw if isinstance(raw, list) else [raw]
            texts, visited = [], []
            for result in results[:self.max_pages]:
                page_url = getattr(result, "url", "")
                if page_url:
                    visited.append(page_url)
                if not getattr(result, "success", False):
                    continue
                markdown = getattr(result, "markdown", "")
                if hasattr(markdown, "fit_markdown"):
                    markdown = markdown.fit_markdown or markdown.raw_markdown or ""
                elif not isinstance(markdown, str):
                    markdown = getattr(markdown, "raw_markdown", "") or ""
                if not markdown:
                    markdown = clean_html(getattr(result, "cleaned_html", "") or getattr(result, "html", ""))
                if markdown:
                    texts.append(str(markdown))
            text = re.sub(r"\s+", " ", " ".join(texts)).strip()
            return CrawlResult(len(text) >= self.min_text_length, url, text[:12000], visited,
                               "" if len(text) >= self.min_text_length else "insufficient extracted text", "crawl4ai")
        except Exception as exc:
            log.info("policy_crawl_crawl4ai_failed url=%s error=%s", url, exc)
            return CrawlResult(False, url, error=str(exc), engine="crawl4ai")

    async def _crawl_async(self, url: str):
        from crawl4ai import AsyncWebCrawler
        from crawl4ai.async_configs import BrowserConfig, CacheMode, CrawlerRunConfig
        from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
        config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS,
            deep_crawl_strategy=BFSDeepCrawlStrategy(max_depth=self.max_depth, include_external=False, max_pages=self.max_pages),
            exclude_external_links=True, remove_overlay_elements=True, page_timeout=int(self.timeout * 1000))
        async with AsyncWebCrawler(config=BrowserConfig(headless=True)) as crawler:
            return await crawler.arun(url=url, config=config)

    @staticmethod
    def _run(coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        result = {}
        def runner():
            try:
                result["value"] = asyncio.run(coro)
            except Exception as exc:
                result["error"] = exc
        thread = threading.Thread(target=runner, daemon=True)
        thread.start(); thread.join()
        if "error" in result:
            raise result["error"]
        return result["value"]


class HybridPolicyCrawler:
    def __init__(self, primary, fallback):
        self.primary, self.fallback = primary, fallback

    def crawl(self, url: str) -> CrawlResult:
        engine = os.getenv("POLICY_CRAWLER_ENGINE", "hybrid").strip().lower()
        if engine == "crawl4ai":
            return self.fallback.crawl(url)
        first = self.primary.crawl(url)
        if first.success or engine == "requests":
            return first
        log.info("policy_crawl_fallback engine=crawl4ai url=%s reason=%s", url, first.error)
        second = self.fallback.crawl(url)
        if second.success:
            second.visited_urls = list(dict.fromkeys(first.visited_urls + second.visited_urls))
            return second
        return CrawlResult(False, url, second.text or first.text,
                           list(dict.fromkeys(first.visited_urls + second.visited_urls)),
                           f"requests: {first.error}; crawl4ai: {second.error}", "hybrid")


def policy_crawler():
    pages = max(1, int(os.getenv("POLICY_CRAWL_MAX_PAGES", "4")))
    depth = max(0, int(os.getenv("POLICY_CRAWL_MAX_DEPTH", "1")))
    timeout = max(1.0, float(os.getenv("POLICY_CRAWL_TIMEOUT", "8")))
    minimum = max(1, int(os.getenv("POLICY_CRAWL_MIN_TEXT", "120")))
    return HybridPolicyCrawler(
        RequestsPolicyCrawler(max_pages=pages, max_depth=depth, timeout=timeout, min_text_length=minimum),
        Crawl4AIPolicyCrawler(max_pages=pages, max_depth=depth, timeout=timeout, min_text_length=minimum),
    )
