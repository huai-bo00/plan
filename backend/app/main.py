from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv, set_key
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request

from app.data import ingest, load_records
from app.memory import add_turn, clear_history, get_history
from app.service import run_query

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
os.environ.setdefault("LLM_BASE_URL", "https://api.openai.com/v1")
os.environ.setdefault("LLM_MODEL", "gpt-4o-mini")
os.environ.setdefault("LLM_ENABLED", "false")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = FastAPI(title="矿业情报问答台", version="1.0.0", description="矿业新闻、关键矿产政策与市场价格问答 Demo")
app.mount("/static", StaticFiles(directory=ROOT / "frontend" / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "frontend")


class QueryRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    session_id: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class LLMSettingsRequest(BaseModel):
    api_key: str = Field(default="", max_length=2000)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/health")
def health():
    return {"status": "ok", "records": len(load_records())}


@app.post("/query")
def query(body: QueryRequest):
    started = time.perf_counter()
    try:
        session_id = body.session_id or uuid.uuid4().hex
        history = get_history(session_id)
        result = run_query(body.question, history=history)
        add_turn(session_id, body.question, result["answer"])
        result["session_id"] = session_id
        result["meta"]["memory_turns"] = len(get_history(session_id))
        logging.getLogger("mining_demo").info("api_query_done trace_id=%s duration_ms=%.1f", result["trace_id"], (time.perf_counter()-started)*1000)
        return result
    except Exception as exc:
        logging.exception("query_failed")
        raise HTTPException(status_code=500, detail="查询失败，请检查数据文件或服务日志。") from exc


@app.post("/ingest")
def refresh_data():
    """Manually refresh provider data; intended for local demo use."""
    trace_id = f"ing_{uuid.uuid4().hex[:12]}"
    result = ingest(trace_id=trace_id)
    result["trace_id"] = trace_id
    return result


@app.get("/api/stats")
def stats():
    records = load_records()
    return {"total": len(records), "by_type": {k: sum(r.category == k for r in records) for k in ("news", "policy", "price")}, "demo_records": sum(r.data_quality != "live" for r in records)}


@app.get("/api/settings/llm")
def get_llm_settings():
    key = os.getenv("LLM_API_KEY", "")
    return {
        "enabled": os.getenv("LLM_ENABLED", "false").lower() == "true",
        "key_configured": bool(key),
    }


@app.put("/api/settings/llm")
def save_llm_settings(body: LLMSettingsRequest):
    api_key = body.api_key.strip()
    if not api_key and not os.getenv("LLM_API_KEY"):
        raise HTTPException(status_code=422, detail="请先填写 API Key。")
    values = {
        "LLM_ENABLED": "true",
        "LLM_BASE_URL": os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1",
        "LLM_MODEL": os.getenv("LLM_MODEL") or "gpt-4o-mini",
    }
    if api_key:
        values["LLM_API_KEY"] = api_key
    try:
        env_path = ROOT / ".env"
        for key, value in values.items():
            set_key(str(env_path), key, value, quote_mode="always")
            os.environ[key] = value
    except OSError as exc:
        logging.getLogger("mining_demo").exception("llm_settings_save_failed")
        raise HTTPException(status_code=500, detail="配置写入失败，请检查项目目录权限。") from exc
    return {"saved": True, "enabled": True, "message": "LLM 已启用；新设置已生效。"}


@app.get("/api/memory")
def read_memory(session_id: str = Query(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")):
    return {"session_id": session_id, "turns": get_history(session_id)}


@app.delete("/api/memory")
def delete_memory(session_id: str = Query(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")):
    clear_history(session_id)
    return {"session_id": session_id, "turns": 0, "cleared": True}
