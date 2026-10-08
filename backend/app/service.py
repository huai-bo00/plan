from __future__ import annotations

import logging
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, TypedDict

import requests
from langgraph.graph import END, StateGraph

from app.data import Record, load_records
from app.rag import retrieve as retrieve_rag

log = logging.getLogger("mining_demo")
TERMS = {
    "lithium": ["锂", "锂矿", "锂价", "碳酸锂", "lithium"],
    "copper": ["铜", "铜价", "copper", "HG=F"],
    "nickel": ["镍", "镍价", "nickel"],
    "rare earth": ["稀土", "rare earth"],
    "critical minerals": ["关键矿产", "critical mineral"],
    "iron ore": ["铁矿", "iron ore"],
    "gold": ["金", "黄金", "gold"],
    "silver": ["银", "白银", "silver"],
}
TOPIC_FILTERS = {
    "锂矿": ("news", ("锂矿", "lithium mine", "lithium supply")),
    "电池材料": ("news", ("电池材料", "battery material")),
    "矿业投资": ("news", ("矿业投资", "mining investment")),
    "资源开发": ("news", ("资源开发", "resource development")),
    "供应链": ("news", ("供应链", "supply chain")),
    "关键矿产": ("policy", ("关键矿产", "critical mineral")),
    "矿业许可": ("policy", ("矿业许可", "mining permit")),
    "资源安全": ("policy", ("资源安全", "resource security")),
    "清洁能源": ("policy", ("清洁能源", "clean energy")),
    "循环利用": ("policy", ("循环利用", "recycling")),
}


class QueryState(TypedDict, total=False):
    question: str
    context_question: str
    history: list[dict[str, str]]
    intent: str
    needs_rag: bool
    rag_status: str
    rag_hits: list[dict[str, Any]]
    rag_error: str
    records: list[Record]
    now: date
    trace_id: str
    timings_ms: dict[str, float]
    days: int
    start_date: date
    commodity: str | None
    query_terms: set[str]
    asks_news: bool
    asks_policy: bool
    asks_price: bool
    active_topics: list[tuple[str, tuple[str, ...]]]
    candidates: list[tuple[int, Record]]
    retrieved: list[Record]
    trend: dict[str, Any] | None
    answer: str
    answer_evidence: list[dict[str, Any]]
    llm_used: bool
    result: dict[str, Any]


def _timed_node(name: str, fn):
    def wrapped(state: QueryState) -> dict[str, Any]:
        started = time.perf_counter()
        update = fn(state)
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        timings = dict(state.get("timings_ms", {}))
        timings[name] = elapsed
        log.info("query_stage_done trace_id=%s stage=%s duration_ms=%.2f status=ok", state["trace_id"], name, elapsed)
        return {**update, "timings_ms": timings}
    return wrapped


def _parse_query(state: QueryState) -> dict[str, Any]:
    question = state["question"]
    context_question = question
    history = state.get("history", [])
    if history and re.search(r"它|这个|这些|那为什么|为什么|怎么|继续|再说说|详细一点|展开说", question):
        context_question = f"{history[-1]['question']} {question}"
    days = _extract_days(context_question) or 30
    commodity = _commodity(context_question)
    asks_news = any(x in context_question.lower() for x in ("新闻", "news"))
    asks_policy = any(x in context_question.lower() for x in ("政策", "法规", "policy"))
    asks_price = bool(commodity) or any(x in context_question.lower() for x in ("价格", "走势", "涨", "跌", "price"))
    mining_related = bool(commodity) or bool(re.search(r"矿业|矿山|矿产|矿石|矿物|冶炼|采矿|选矿|mining|mineral|ore|smelt|refin|电池|battery|cathode|anode|有色金属|能源金属|供应链|资源安全", context_question, re.I))
    asks_current_mining = mining_related and bool(re.search(r"最近|过去|近\s*\d+\s*天|今天|本周|本月|今年|最新|当前|新闻|政策|价格|走势|涨|跌|price|news|policy", context_question, re.I))
    asks_general = bool(re.search(r"[?？]|吗|呢|什么|为什么|怎么|如何|是否|能不能|可以吗|介绍|解释|区别|原理|讲讲|讲个|请|帮我|给我|总结|翻译|推荐|what |who |where |when |why |how |explain|define|tell me|write|summarize|translate", context_question, re.I))
    return {
        "context_question": context_question,
        "intent": "general_qa" if asks_general and not asks_current_mining else "mining_analysis",
        "needs_rag": mining_related and os.getenv("RAG_ENABLED", "true").lower() == "true",
        "days": days,
        "start_date": state["now"] - timedelta(days=days - 1),
        "commodity": commodity,
        "query_terms": set(_tokens(context_question)),
        "asks_news": asks_news,
        "asks_policy": asks_policy,
        "asks_price": asks_price,
        "active_topics": [(category, aliases) for phrase, (category, aliases) in TOPIC_FILTERS.items() if phrase in context_question],
    }


def _rag_retrieve(state: QueryState) -> dict[str, Any]:
    if not (os.getenv("RAG_API_KEY") or os.getenv("LLM_API_KEY")):
        return {"rag_hits": [], "rag_status": "skipped_no_api_key", "rag_error": "未配置 Embedding API Key"}
    try:
        hits = retrieve_rag(state.get("context_question", state["question"]), state["records"], k=int(os.getenv("RAG_TOP_K", "5")))
        log.info("rag_retrieval_done trace_id=%s hits=%d", state["trace_id"], len(hits))
        return {"rag_hits": hits, "rag_status": "ok" if hits else "empty", "rag_error": ""}
    except Exception as exc:
        log.warning("rag_retrieval_failed trace_id=%s error=%s", state["trace_id"], exc)
        return {"rag_hits": [], "rag_status": "error", "rag_error": str(exc)[:300]}


def _retrieve_evidence(state: QueryState) -> dict[str, Any]:
    candidates: list[tuple[int, Record]] = []
    if state.get("intent") == "general_qa":
        return {"candidates": candidates, "retrieved": []}
    question = state.get("context_question", state["question"])
    for record in state["records"]:
        if record.published_at < state["start_date"].isoformat() or record.published_at > state["now"].isoformat():
            continue
        lower = f"{record.title} {record.content} {record.commodity or ''} {record.category}".lower()
        matching_topics = [(category, aliases) for category, aliases in state["active_topics"] if category == record.category]
        if matching_topics and not any(any(alias.lower() in lower for alias in aliases) for _, aliases in matching_topics):
            continue
        commodity = state.get("commodity")
        if commodity and record.category == "price" and commodity.lower() not in lower and not any(term.lower() in lower for term in TERMS.get(commodity, [])):
            continue
        overlap = sum(1 for term in state["query_terms"] if term and term in lower)
        if state["asks_news"] and record.category == "news":
            overlap += 3
        if state["asks_policy"] and record.category == "policy":
            overlap += 3
        if state["asks_price"] and record.category == "price":
            overlap += 2
        if record.category == "price" and commodity and any(term.lower() in question.lower() for term in TERMS.get(commodity, [])):
            overlap += 3
        if overlap or (not state["query_terms"] and record.category == "price"):
            candidates.append((overlap, record))
    candidates.sort(key=lambda item: (item[0], item[1].published_at), reverse=True)
    retrieved: list[Record] = []
    per_category = {"news": 0, "policy": 0, "price": 0}
    for _, record in candidates:
        if per_category.get(record.category, 0) >= 2:
            continue
        retrieved.append(record)
        per_category[record.category] = per_category.get(record.category, 0) + 1
        if len(retrieved) == 5:
            break
    return {"candidates": candidates, "retrieved": retrieved}


def _calculate_price(state: QueryState) -> dict[str, Any]:
    trend = None if state.get("intent") == "general_qa" else _trend(state["records"], state.get("commodity"), state["start_date"], state["now"])
    retrieved = list(state["retrieved"])
    if state["asks_price"] and trend and trend.get("available"):
        by_id = {record.id: record for record in state["records"]}
        endpoints = [by_id[rid] for rid in (trend["first_record_id"], trend["last_record_id"]) if rid in by_id]
        endpoint_ids = {record.id for record in endpoints}
        fill = [record for _, record in state["candidates"] if record.id not in endpoint_ids]
        retrieved = endpoints + fill[:max(0, 5 - len(endpoints))]
    retrieved_ids = {record.id for record in retrieved}
    rag_hits = [hit for hit in state.get("rag_hits", []) if not hit.get("id") or hit["id"] not in retrieved_ids]
    return {"trend": trend, "retrieved": retrieved, "rag_hits": rag_hits}


def _compose_answer(state: QueryState) -> dict[str, Any]:
    rag_hits = state.get("rag_hits", [])
    if state.get("intent") == "general_qa" and not rag_hits:
        answer = "这是一个基础问答问题。配置 API Key 后可启用模型回答；当前也可以询问矿业新闻、政策或行情分析。"
    else:
        answer = _answer(state["question"], state["retrieved"], state.get("trend"), state["days"], state.get("commodity"))
    if rag_hits:
        rag_summary = "知识库参考资料：" + "；".join(
            f"{hit['title']}：{hit['snippet'][:180]}" for hit in rag_hits[:2]
        ) + "。"
        if "没有找到足够相关的资料" in answer or "基础问答问题" in answer:
            answer = rag_summary
        elif not state.get("llm_used"):
            answer += rag_summary
    return {"answer": answer}


def _optional_llm_node(state: QueryState) -> dict[str, Any]:
    direct_general = state.get("intent") == "general_qa" and not state.get("needs_rag")
    rag_fallback = bool(state.get("needs_rag") and not state.get("rag_hits") and not state.get("retrieved"))
    answer, used = _optional_llm(
        state["question"], state["answer"], state["retrieved"], state.get("history", []),
        direct_general, state.get("rag_hits", []), rag_fallback,
    )
    return {"answer": answer, "llm_used": used}


def _finalize(state: QueryState) -> dict[str, Any]:
    retrieved = state["retrieved"]
    timings = dict(state.get("timings_ms", {}))
    timings["finalize"] = 0.0
    timings["total"] = round(sum(value for key, value in timings.items() if key != "total"), 2)
    log.info("query_trace trace_id=%s retrieved=%d range_days=%d total_ms=%.2f llm=%s", state["trace_id"], len(retrieved), state["days"], timings["total"], state.get("llm_used", False))
    output_sources = [as_source(record, index + 1) for index, record in enumerate(retrieved)]
    seen_ids = {source["id"] for source in output_sources}
    for hit in state.get("rag_hits", []):
        if hit.get("id") and hit["id"] in seen_ids:
            continue
        output_sources.append({**hit, "rank": len(output_sources) + 1})
        if hit.get("id"):
            seen_ids.add(hit["id"])
    graph_order = ["parse_query", "rag_retrieve", "retrieve_evidence", "calculate_price", "compose_answer", "optional_llm", "finalize"]
    graph_nodes = [node for node in graph_order if node == "finalize" or node in timings]
    evidence = _answer_evidence(retrieved, state.get("trend"))
    record_ids = {record.id for record in state["records"]}
    evidence.extend({"claim": hit["title"], "source_ids": [hit["id"]]} for hit in state.get("rag_hits", []) if hit.get("id") in record_ids and hit.get("title") in state["answer"])
    return {"result": {
        "trace_id": state["trace_id"],
        "question": state["question"],
        "answer": state["answer"],
        "time_range": {"start": state["start_date"].isoformat(), "end": state["now"].isoformat(), "days": state["days"]},
        "price_change": state.get("trend"),
        "sources": output_sources,
        "answer_evidence": evidence,
        "meta": {"retrieval_count": len(output_sources), "rag_count": len(state.get("rag_hits", [])), "rag_status": state.get("rag_status", "not_needed"), "llm_used": state.get("llm_used", False), "timings_ms": timings,
                 "answer_mode": state.get("intent", "mining_analysis"),
                 "graph_nodes": graph_nodes},
    }}


def build_query_graph():
    graph = StateGraph(QueryState)
    graph.add_node("parse_query", _timed_node("parse_query", _parse_query))
    graph.add_node("rag_retrieve", _timed_node("rag_retrieve", _rag_retrieve))
    graph.add_node("retrieve_evidence", _timed_node("retrieve_evidence", _retrieve_evidence))
    graph.add_node("calculate_price", _timed_node("calculate_price", _calculate_price))
    graph.add_node("compose_answer", _timed_node("compose_answer", _compose_answer))
    graph.add_node("optional_llm", _timed_node("optional_llm", _optional_llm_node))
    graph.add_node("finalize", _timed_node("finalize", _finalize))
    graph.set_entry_point("parse_query")
    graph.add_conditional_edges("parse_query", lambda state: "rag_retrieve" if state.get("needs_rag") else "retrieve_evidence", {"rag_retrieve": "rag_retrieve", "retrieve_evidence": "retrieve_evidence"})
    graph.add_edge("rag_retrieve", "retrieve_evidence")
    graph.add_edge("retrieve_evidence", "calculate_price")
    graph.add_edge("calculate_price", "compose_answer")
    graph.add_edge("compose_answer", "optional_llm")
    graph.add_edge("optional_llm", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


QUERY_GRAPH = build_query_graph()


def run_query(question: str, records: list[Record] | None = None, *, now: date | None = None, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    trace_id = f"q_{uuid.uuid4().hex[:12]}"
    state: QueryState = {
        "question": question,
        "records": records if records is not None else load_records(),
        "now": now or date.today(),
        "history": history or [],
        "trace_id": trace_id,
        "timings_ms": {},
    }
    return QUERY_GRAPH.invoke(state)["result"]


def _trend(records: list[Record], commodity: str | None, start: date, end: date) -> dict[str, Any] | None:
    if not commodity:
        return None
    candidates = [r for r in records if r.category == "price" and r.commodity and commodity.lower() in r.commodity.lower() and r.value is not None and start.isoformat() <= r.published_at <= end.isoformat()]
    series: dict[tuple[str, str], list[Record]] = defaultdict(list)
    for record in candidates:
        series[(record.source, record.unit or "")].append(record)
    prices = max(series.values(), key=lambda rows: (len(rows), max(r.published_at for r in rows)), default=[])
    prices.sort(key=lambda r: r.published_at)
    if len(prices) < 2:
        return {"commodity": commodity, "available": False, "message": "所选时间范围内价格记录不足，暂不能计算变化。"}
    first, last = prices[0], prices[-1]
    delta = last.value-first.value
    percent = (delta/first.value*100) if first.value else None
    return {"commodity": first.commodity, "available": True, "first_date": first.published_at, "last_date": last.published_at, "first_value": first.value, "last_value": last.value, "absolute_change": round(delta, 4), "percent_change": round(percent, 2) if percent is not None else None, "unit": first.unit, "data_quality": "illustrative" if any(p.data_quality != "live" for p in prices) else "live", "first_record_id": first.id, "last_record_id": last.id, "points": [{"date": p.published_at, "value": p.value} for p in prices]}


def _answer(question: str, sources: list[Record], trend: dict[str, Any] | None, days: int, commodity: str | None) -> str:
    if not sources and not (trend and trend.get("available")):
        return "当前数据集中没有找到足够相关的资料，暂不作推断。可以更换矿种关键词、扩大时间范围，或先运行数据采集。"
    parts = [f"检索范围：最近 {days} 天。"]
    if trend and trend.get("available"):
        direction = "上涨" if trend["absolute_change"] > 0 else "下跌" if trend["absolute_change"] < 0 else "持平"
        label = "（演示样例，非实时行情）" if trend.get("data_quality") == "illustrative" else ""
        parts.append(f"{trend['commodity']}价格从 {trend['first_value']} {trend['unit']}（{trend['first_date']}）变为 {trend['last_value']} {trend['unit']}（{trend['last_date']}），区间{direction} {abs(trend['percent_change'])}%{label}。")
    elif commodity:
        parts.append(f"没有足够的{commodity}价格点计算区间变化。")
    docs = [r for r in sources if r.category != "price"]
    if docs:
        parts.append("相关信息：" + "；".join(f"{r.title}（{r.source}，{r.published_at}）" for r in docs[:3]) + "。")
    prices = [r for r in sources if r.category == "price"]
    if prices and not trend:
        parts.append("检索到价格记录，但未能确定可比较的同一商品区间。")
    return "".join(parts)


def _optional_llm(question: str, answer: str, sources: list[Record], history: list[dict[str, str]], general_qa: bool, rag_sources: list[dict[str, Any]] | None = None, rag_fallback: bool = False) -> tuple[str, bool]:
    rag_sources = rag_sources or []
    if os.getenv("LLM_ENABLED", "false").lower() != "true" or not os.getenv("LLM_API_KEY") or not os.getenv("LLM_BASE_URL") or (not sources and not rag_sources and not general_qa and not rag_fallback):
        return answer, False
    base = os.getenv("LLM_BASE_URL").rstrip("/")
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    model = os.getenv("LLM_MODEL", "")
    history_text = "\n".join(f"用户：{turn['question']}\n助手：{turn['answer']}" for turn in history[-8:])
    if general_qa:
        prompt = "你是一个可靠的中文基础问答助手。直接、清楚地回答常识和概念问题；需要时分点说明。不要把不确定或可能过时的信息说成当前事实。结合对话历史理解追问。\n对话历史：\n" + (history_text or "（无）") + "\n当前问题：" + question
    elif rag_fallback:
        prompt = "用户询问矿业或关键矿产问题，但知识库和结构化数据没有返回证据。可以给出谨慎的一般性背景说明，同时明确说明当前没有检索到可核验资料；不得编造最新行情、新闻或政策事实。结合对话历史理解追问。\n对话历史：\n" + (history_text or "（无）") + "\n当前问题：" + question
    else:
        context_lines = [f"[{i+1}] {r.title}; {r.published_at}; {r.source}; {r.content[:800]}" for i, r in enumerate(sources)]
        offset = len(context_lines)
        context_lines.extend(f"[{offset+i+1}] {hit.get('title', '知识库片段')}; {hit.get('date', '')}; {hit.get('source', '')}; {hit.get('snippet', '')[:1000]}" for i, hit in enumerate(rag_sources))
        prompt = "Answer in concise Chinese using only the supplied evidence. Do not add unsupported facts. Cite source numbers [1], [2] inline. If the evidence is insufficient, say so. Use conversation history to resolve follow-up references.\nConversation history:\n" + (history_text or "（无）") + "\nQuestion: " + question + "\nRetrieved evidence:\n" + "\n".join(context_lines) + "\nDeterministic price calculation: " + answer
    try:
        response = requests.post(endpoint, headers={"Authorization": f"Bearer {os.getenv('LLM_API_KEY')}"}, json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}, timeout=20)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        return (content, True) if content else (answer, False)
    except Exception as exc:
        log.warning("llm_fallback error=%s", exc)
        return answer, False


def as_source(r: Record, rank: int) -> dict[str, Any]:
    return {"rank": rank, "id": r.id, "type": r.category, "title": r.title, "snippet": r.content[:360], "date": r.published_at, "source": r.source, "url": r.url, "commodity": r.commodity, "value": r.value, "unit": r.unit, "data_quality": r.data_quality}


def _extract_days(q: str) -> int | None:
    m = re.search(r"最近\s*(\d+)\s*天|过去\s*(\d+)\s*天|近\s*(\d+)\s*天", q)
    return min(int(next(x for x in m.groups() if x)), 365) if m else None


def _commodity(q: str) -> str | None:
    low = q.lower()
    for name, aliases in TERMS.items():
        if any(alias.lower() in low for alias in aliases):
            return name
    return None


def _tokens(q: str) -> list[str]:
    chunks = re.findall(r"[a-zA-Z]{3,}|[\u4e00-\u9fff]+", q.lower())
    stop = {"过去", "最近", "变化", "有什么", "如何", "相关", "新闻", "政策", "情况", "价格", "走势", "有哪些", "多少", "矿价", "什么", "近", "天"}
    words = []
    for chunk in chunks:
        if re.fullmatch(r"[a-z]+", chunk):
            words.append(chunk)
        else:
            words.extend(chunk[i:i+2] for i in range(len(chunk)-1))
    return [word for word in words if word not in stop]


def _answer_evidence(sources: list[Record], trend: dict[str, Any] | None) -> list[dict[str, Any]]:
    evidence = [{"claim": record.title, "source_ids": [record.id]} for record in [item for item in sources if item.category != "price"][:3]]
    if trend and trend.get("available"):
        evidence.append({"claim": f"{trend['commodity']} price changed {trend['percent_change']}%", "source_ids": [trend["first_record_id"], trend["last_record_id"]]})
    return evidence
