from datetime import date

from app.data import OfficialCSVPriceProvider, Record, ingest, load_records, normalize_dedupe
from app.service import QUERY_GRAPH, run_query


def rec(id, category, title, day, **kwargs):
    return Record(id, category, title, kwargs.get("content", title), day.isoformat(), "test", "https://example.test/"+id, kwargs.get("commodity"), kwargs.get("value"), kwargs.get("unit"), kwargs.get("data_quality", "live"))


def test_normalize_dates_and_dedupe_by_url():
    day = date(2026, 10, 8)
    records = [rec("a", "news", "  A   headline ", day), rec("b", "news", "duplicate", day)]
    records[1].url = records[0].url
    result = normalize_dedupe(records)
    assert len(result) == 1
    assert result[0].title == "A headline"


def test_price_dedup_keeps_separate_units_and_sources():
    day = date(2026, 10, 8)
    a = rec("a", "price", "Copper", day, commodity="copper", value=8, unit="USD/lb")
    b = rec("b", "price", "Copper", day, commodity="copper", value=8800, unit="USD/tonne")
    a.url = b.url = "https://example.test/market"
    assert len(normalize_dedupe([a, b])) == 2


def test_official_csv_provider_imports_price_rows(tmp_path):
    csv_file = tmp_path/"prices.csv"
    csv_file.write_text("date,commodity,value,unit,source,url\n2026-10-08,Lithium Carbonate,72000,CNY/tonne,Exchange CSV,https://example.test/source\n", encoding="utf-8")
    records = OfficialCSVPriceProvider("official CSV", csv_file, "https://example.test").fetch()
    assert len(records) == 1
    assert records[0].value == 72000
    assert records[0].source == "Exchange CSV"


def test_illustrative_fixture_rolls_to_current_date(tmp_path):
    records = load_records(tmp_path/"not-yet-created.json")
    newest_demo_day = max(date.fromisoformat(r.published_at) for r in records if r.data_quality == "illustrative")
    assert newest_demo_day == date.today()


def test_query_computes_price_range_and_cites_sources(monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "false")
    today = date(2026, 10, 8)
    records = [
        rec("p1", "price", "Lithium close", today.replace(day=2), commodity="lithium carbonate", value=100, unit="CNY/t"),
        rec("p2", "price", "Lithium close", today, commodity="lithium carbonate", value=110, unit="CNY/t"),
        rec("n1", "news", "Lithium supply update", today, content="lithium supply update"),
        rec("pol1", "policy", "Critical minerals policy", today, content="critical minerals policy"),
    ]
    result = run_query("过去 7 天锂价有什么变化？有哪些相关政策或新闻？", records, now=today)
    assert result["price_change"]["available"]
    assert result["price_change"]["percent_change"] == 10.0
    assert {s["type"] for s in result["sources"]} >= {"news", "policy", "price"}
    assert result["answer_evidence"]


def test_follow_up_uses_last_question_as_memory_context(monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "false")
    today = date(2026, 10, 8)
    records = [
        rec("p1", "price", "Lithium close", today.replace(day=2), commodity="lithium carbonate", value=100, unit="CNY/t"),
        rec("p2", "price", "Lithium close", today, commodity="lithium carbonate", value=110, unit="CNY/t"),
    ]
    result = run_query("那它为什么上涨？", records, now=today, history=[{"question": "过去 7 天锂价有什么变化？", "answer": "锂价上涨 10%。"}])
    assert result["price_change"]["available"]
    assert result["meta"]["retrieval_count"] >= 2


def test_query_abstains_when_no_evidence():
    result = run_query("深海量子种植星际航线", [], now=date(2026, 10, 8))
    assert result["sources"] == []
    assert "没有找到足够" in result["answer"]


def test_langgraph_query_nodes_and_timings(monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "false")
    result = run_query("过去 7 天锂价有什么变化？有哪些相关政策或新闻？", now=date(2026, 10, 8))
    assert result["meta"]["graph_nodes"] == ["parse_query", "retrieve_evidence", "calculate_price", "compose_answer", "optional_llm", "finalize"]
    assert set(result["meta"]["timings_ms"]) == {*result["meta"]["graph_nodes"], "total"}
    assert {node.name for node in QUERY_GRAPH.get_graph().nodes.values()} >= set(result["meta"]["graph_nodes"])


def test_rag_node_runs_for_mining_knowledge_question(monkeypatch):
    import app.service as service

    monkeypatch.setenv("RAG_ENABLED", "true")
    monkeypatch.setenv("LLM_API_KEY", "embedding-test-key")
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setattr(service, "retrieve_rag", lambda query, records, k: [{
        "id": "policy-1", "type": "policy", "title": "锂矿政策知识", "snippet": "锂矿项目需要依法办理许可。",
        "date": "2026-10-08", "source": "示例法规", "url": "https://example.test/policy",
        "data_quality": "live", "distance": 0.12,
    }])
    result = run_query("锂矿项目需要哪些许可？", records=[], now=date(2026, 10, 8))
    assert "rag_retrieve" in result["meta"]["graph_nodes"]
    assert result["meta"]["rag_count"] == 1
    assert result["sources"][0]["title"] == "锂矿政策知识"
    assert "依法办理许可" in result["answer"]


def test_unrelated_general_question_bypasses_rag(monkeypatch):
    import app.service as service

    monkeypatch.setenv("RAG_ENABLED", "true")
    monkeypatch.setattr(service, "retrieve_rag", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("RAG should be bypassed")))
    result = run_query("太阳系最大的行星是什么？", records=[], now=date(2026, 10, 8))
    assert "rag_retrieve" not in result["meta"]["graph_nodes"]
    assert result["meta"]["rag_status"] == "not_needed"


def test_general_qa_uses_llm_and_memory_context(monkeypatch):
    import app.service as service

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "木星是太阳系最大的行星。"}}]}

    captured = {}
    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["prompt"] = kwargs["json"]["messages"][0]["content"]
        return FakeResponse()

    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setattr(service.requests, "post", fake_post)
    history = [{"question": "我想了解矿业基础概念", "answer": "可以问我矿业术语。"}]
    result = run_query("太阳系最大的行星是什么？", records=[], history=history)
    assert result["answer"].startswith("木星是")
    assert result["meta"]["llm_used"] is True
    assert result["meta"]["answer_mode"] == "general_qa"
    assert "我想了解矿业基础概念" in captured["prompt"]
    assert "太阳系最大的行星是什么" in captured["prompt"]


class FakeProvider:
    name = "test feed"
    category = "news"
    def __init__(self, fail=False): self.fail = fail
    def fetch(self):
        if self.fail: raise RuntimeError("offline")
        return [Record("x", "news", "one", "content", "2026-10-08", "fake", "https://example.test/x")]


def test_provider_failure_uses_its_cache(tmp_path):
    data_path, cache_path = tmp_path/"data.json", tmp_path/"cache"
    first = ingest([FakeProvider()], data_path=data_path, cache_path=cache_path)
    second = ingest([FakeProvider(fail=True)], data_path=data_path, cache_path=cache_path)
    assert first["providers"][0]["status"] == "ok"
    assert second["providers"][0]["status"] == "cached"
    assert any(r.id == "x" for r in __import__("app.data", fromlist=["load_records"]).load_records(data_path))
