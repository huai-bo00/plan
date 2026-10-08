from app.data import Record
from app.rag import _corpus, _embed


def test_rag_corpus_uses_news_policy_and_local_text_but_skips_prices(tmp_path, monkeypatch):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "mining.md").write_text("# Mining\nLithium supply chains need permits.", encoding="utf-8")
    monkeypatch.setenv("RAG_KNOWLEDGE_DIR", str(knowledge))
    monkeypatch.setenv("RAG_CHUNK_SIZE", "300")
    policy = Record("pol-1", "policy", "Mineral policy", "Critical minerals licensing rules.", "2026-10-08", "Agency", "https://example.test/policy")
    price = Record("price-1", "price", "Lithium", "price point", "2026-10-08", "Market", "https://example.test/price")
    docs = _corpus([policy, price])
    assert {doc["category"] for doc in docs} == {"policy", "knowledge"}
    assert any(doc["record_id"] == "pol-1" for doc in docs)
    assert any(doc["source"] == "mining.md" for doc in docs)


def test_embedding_adapter_calls_openai_compatible_endpoint(monkeypatch):
    import app.rag as rag

    captured = {}
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"index": 0, "embedding": [0.1, 0.2]}, {"index": 1, "embedding": [0.3, 0.4]}]}
    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("RAG_EMBEDDING_BASE_URL", "https://embeddings.example/v1")
    monkeypatch.setenv("RAG_EMBEDDING_MODEL", "test-embed")
    monkeypatch.setattr(rag.requests, "post", fake_post)
    assert _embed(["第一段", "第二段"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert captured["url"] == "https://embeddings.example/v1/embeddings"
    assert captured["json"]["model"] == "test-embed"
    assert captured["headers"]["Authorization"] == "Bearer test-key"


def test_chroma_index_and_vector_retrieval_with_fake_embeddings(tmp_path, monkeypatch):
    import pytest
    pytest.importorskip("chromadb")
    import app.rag as rag

    monkeypatch.setenv("RAG_PERSIST_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("RAG_COLLECTION", "test-mining-rag")
    monkeypatch.setenv("RAG_MAX_DISTANCE", "2")
    monkeypatch.setenv("RAG_KNOWLEDGE_DIR", str(tmp_path / "empty-knowledge"))
    embedding_batches = []
    def fake_embed(texts):
        embedding_batches.append(len(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]
    monkeypatch.setattr(rag, "_embed", fake_embed)
    record = Record("policy-vector-1", "policy", "Critical mineral licensing", "Projects need a license before extraction.", "2026-10-08", "Mining office", "https://example.test/license")
    hits = rag.retrieve("What permits are needed for mineral extraction?", [record], k=3)
    assert len(hits) == 1
    assert hits[0]["id"] == record.id
    assert hits[0]["type"] == "policy"
    assert rag.retrieve("What permits are needed for mineral extraction?", [record], k=3)
    assert embedding_batches == [1, 1, 1]  # document vector only computed on initial index build
