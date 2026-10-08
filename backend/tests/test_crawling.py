from types import SimpleNamespace

from app.crawling import CrawlResult, HybridPolicyCrawler, RequestsPolicyCrawler, clean_html


def test_clean_html_removes_navigation_and_scripts():
    result = clean_html("<nav>menu</nav><main><h1>Policy</h1><p>Critical minerals rules and strategy.</p></main><script>bad()</script><footer>copyright</footer>")
    assert "Critical minerals rules" in result
    assert "menu" not in result
    assert "bad()" not in result


def test_requests_crawler_extracts_main_text_and_policy_links(monkeypatch):
    import app.crawling as crawling

    html = "<nav>menu</nav><main><h1>Critical Minerals Policy</h1><p>" + "Official policy content. " * 20 + "</p><a href='/critical-minerals/strategy'>Strategy</a></main>"
    calls = []
    def fake_get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=200, headers={"Content-Type": "text/html"}, text=html,
                               raise_for_status=lambda: None)
    monkeypatch.setattr(crawling.requests, "get", fake_get)
    result = RequestsPolicyCrawler(max_pages=2, max_depth=1, min_text_length=50).crawl("https://example.gov/policy")
    assert result.success
    assert len(calls) == 2
    assert all("example.gov" in url for url in calls)
    assert "menu" not in result.text


def test_hybrid_crawler_falls_back_when_lightweight_fetch_fails(monkeypatch):
    class FakeCrawler:
        def __init__(self, result): self.result, self.calls = result, []
        def crawl(self, url): self.calls.append(url); return self.result

    monkeypatch.setenv("POLICY_CRAWLER_ENGINE", "hybrid")
    primary = FakeCrawler(CrawlResult(False, "https://example.gov/a", error="403"))
    fallback = FakeCrawler(CrawlResult(True, "https://example.gov/a", "Official policy text" * 20, ["https://example.gov/a"], engine="crawl4ai"))
    result = HybridPolicyCrawler(primary, fallback).crawl("https://example.gov/a")
    assert result.success and result.engine == "crawl4ai"
    assert len(primary.calls) == len(fallback.calls) == 1


def test_hybrid_crawler_returns_failure_when_both_engines_fail(monkeypatch):
    class FakeCrawler:
        def __init__(self, error): self.error = error
        def crawl(self, url): return CrawlResult(False, url, error=self.error)

    monkeypatch.setenv("POLICY_CRAWLER_ENGINE", "hybrid")
    result = HybridPolicyCrawler(FakeCrawler("403"), FakeCrawler("browser unavailable")).crawl("https://example.gov/a")
    assert not result.success
    assert "crawl4ai" in result.error
