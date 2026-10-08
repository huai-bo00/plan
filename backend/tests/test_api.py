import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.main import LLMSettingsRequest, QueryRequest, app, delete_memory, get_llm_settings, health, index, query, read_memory, save_llm_settings


def test_health_and_query_handlers(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_MEMORY_PATH", str(tmp_path / "memory.sqlite3"))
    assert health()["status"] == "ok"
    assert health()["records"] >= 90
    payload = query(QueryRequest(question="过去 7 天锂价有什么变化？有哪些相关政策或新闻？", session_id="test_session"))
    assert payload["trace_id"].startswith("q_")
    assert payload["sources"]
    assert payload["price_change"]["available"]
    assert payload["meta"]["memory_turns"] == 1
    assert read_memory("test_session")["turns"][0]["question"].startswith("过去 7 天")
    assert delete_memory("test_session")["cleared"] is True
    assert read_memory("test_session")["turns"] == []
    routes = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}
    assert ("/query", "POST") in routes
    assert ("/api/memory", "GET") in routes
    assert ("/api/memory", "DELETE") in routes


def test_query_validation_and_frontend_template():
    with pytest.raises(ValidationError):
        QueryRequest(question="")
    scope = {"type": "http", "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/", "raw_path": b"/", "query_string": b"", "headers": [], "client": ("test", 1234), "server": ("test", 80)}
    response = index(Request(scope))
    assert response.status_code == 200
    assert "矿业情报问答台" in response.body.decode("utf-8")


def test_llm_settings_are_saved_without_returning_secret(tmp_path, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    result = save_llm_settings(LLMSettingsRequest(api_key="secret-test-key"))
    assert result["saved"] is True
    assert "secret-test-key" not in str(result)
    assert get_llm_settings() == {"enabled": True, "key_configured": True}
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "secret-test-key" in env_text
    assert "LLM_ENABLED='true'" in env_text
    monkeypatch.delenv("LLM_API_KEY", raising=False)
