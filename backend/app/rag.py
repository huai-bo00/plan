"""Persistent Chroma RAG index for mining news, policy, and local knowledge files."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import requests

from app.data import ROOT, Record

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
log = logging.getLogger("mining_demo")


def _path(value: str, default: Path) -> Path:
    path = Path(value).expanduser() if value else default
    return path if path.is_absolute() else ROOT / path


def _chunks(text: str, size: int, overlap: int) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    step = max(1, size - overlap)
    return [text[start:start + size] for start in range(0, len(text), step)]


def _corpus(records: list[Record]) -> list[dict[str, Any]]:
    size = max(200, int(os.getenv("RAG_CHUNK_SIZE", "900")))
    overlap = min(size - 1, max(0, int(os.getenv("RAG_CHUNK_OVERLAP", "100"))))
    docs: list[dict[str, Any]] = []
    for record in records:
        if record.category not in {"news", "policy"}:
            continue
        body = f"标题：{record.title}\n日期：{record.published_at}\n来源：{record.source}\n正文：{record.content}"
        for index, chunk in enumerate(_chunks(body, size, overlap)):
            docs.append({
                "id": record.id if index == 0 else f"{record.id}#{index}",
                "record_id": record.id,
                "text": chunk,
                "title": record.title,
                "category": record.category,
                "date": record.published_at,
                "source": record.source,
                "url": record.url,
                "data_quality": record.data_quality,
                "chunk": index,
            })

    knowledge_dir = _path(os.getenv("RAG_KNOWLEDGE_DIR", ""), ROOT / "storage" / "knowledge")
    max_file_bytes = max(1024, int(os.getenv("RAG_MAX_FILE_BYTES", str(1_000_000))))
    if knowledge_dir.exists():
        paths = sorted([*knowledge_dir.glob("*.md"), *knowledge_dir.glob("*.txt")])
        for path in paths:
            if path.stat().st_size > max_file_bytes:
                log.warning("rag_skip_oversize_file path=%s bytes=%d", path.name, path.stat().st_size)
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            title = path.stem.replace("_", " ").replace("-", " ")
            source_id = f"knowledge_{hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]}"
            for index, chunk in enumerate(_chunks(f"标题：{title}\n{content}", size, overlap)):
                docs.append({
                    "id": f"{source_id}#{index}", "record_id": "", "text": chunk,
                    "title": title, "category": "knowledge", "date": "",
                    "source": path.name, "url": "", "data_quality": "local", "chunk": index,
                })

    for doc in docs:
        doc["corpus"] = "mining-intelligence"
    return docs


def _fingerprint(docs: list[dict[str, Any]], model: str) -> str:
    digest = hashlib.sha256(model.encode("utf-8"))
    for doc in docs:
        digest.update(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()


def _embedding_url() -> str:
    base = os.getenv("RAG_EMBEDDING_BASE_URL") or os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    base = base.rstrip("/")
    return base if base.endswith("/embeddings") else base + "/embeddings"


def _embed(texts: list[str]) -> list[list[float]]:
    api_key = os.getenv("RAG_API_KEY") or os.getenv("LLM_API_KEY", "")
    if not api_key:
        raise RuntimeError("尚未配置向量 Embedding API Key")
    model = os.getenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small")
    vectors: list[list[float] | None] = [None] * len(texts)
    batch_size = max(1, min(64, int(os.getenv("RAG_EMBEDDING_BATCH_SIZE", "32"))))
    for offset in range(0, len(texts), batch_size):
        batch = texts[offset:offset + batch_size]
        response = requests.post(
            _embedding_url(),
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": batch},
            timeout=max(5, int(os.getenv("RAG_EMBEDDING_TIMEOUT", "45"))),
        )
        response.raise_for_status()
        data = response.json().get("data", [])
        if len(data) != len(batch):
            raise RuntimeError(f"Embedding 返回数量异常：期望 {len(batch)}，收到 {len(data)}")
        for item in data:
            idx = int(item.get("index", 0)) + offset
            vectors[idx] = [float(value) for value in item["embedding"]]
    if any(vector is None for vector in vectors):
        raise RuntimeError("Embedding 响应缺少向量")
    return [vector for vector in vectors if vector is not None]


def _open_collection():
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError("缺少 Chroma 依赖，请运行 pip install -r requirements.txt") from exc
    persist_dir = _path(os.getenv("RAG_PERSIST_DIR", ""), ROOT / "storage" / "chroma")
    persist_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(persist_dir))
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", os.getenv("RAG_COLLECTION", "mining-intelligence")).strip("-_")[:63]
    name = name if len(name) >= 3 else "mining-intelligence"
    # Avoid get_or_create_collection here: Chroma versions may overwrite custom
    # collection metadata on every call, which would invalidate the corpus hash.
    existing = client.list_collections()
    existing_names = {item.name if hasattr(item, "name") else str(item) for item in existing}
    if name in existing_names:
        collection = client.get_collection(name=name, embedding_function=None)
    else:
        # This project always supplies vectors from its configured Embeddings API.
        collection = client.create_collection(name=name, metadata={"hnsw:space": "cosine"}, embedding_function=None)
    return client, collection


def _ensure_index(records: list[Record]):
    client, collection = _open_collection()
    docs = _corpus(records)
    if not docs:
        return collection, 0
    model = os.getenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small")
    fingerprint = _fingerprint(docs, model)
    metadata = collection.metadata or {}
    if metadata.get("corpus_fingerprint") == fingerprint and collection.count() == len(docs):
        return collection, len(docs)

    # Complete embedding before replacing the current collection so API failure
    # cannot destroy the last usable index.
    vectors = _embed([doc["text"] for doc in docs])
    prior_ids = collection.get(include=["metadatas"])["ids"]
    if prior_ids:
        collection.delete(ids=prior_ids)
    batch_size = 128
    for offset in range(0, len(docs), batch_size):
        batch = docs[offset:offset + batch_size]
        collection.add(
            ids=[doc["id"] for doc in batch],
            documents=[doc["text"] for doc in batch],
            embeddings=vectors[offset:offset + batch_size],
            metadatas=[{key: value for key, value in doc.items() if key != "text"} for doc in batch],
        )
    collection.modify(metadata={"corpus_fingerprint": fingerprint})
    log.info("rag_index_rebuilt collection=%s chunks=%d", collection.name, len(docs))
    return collection, len(docs)


def retrieve(query: str, records: list[Record], *, k: int | None = None) -> list[dict[str, Any]]:
    """Ensure the local Chroma index is current, then return relevant evidence chunks."""
    collection, count = _ensure_index(records)
    if not count:
        return []
    query_vector = _embed([query])[0]
    limit = max(1, min(10, int(k or os.getenv("RAG_TOP_K", "5"))))
    result = collection.query(query_embeddings=[query_vector], n_results=min(limit * 3, count), include=["documents", "metadatas", "distances"])
    max_distance = float(os.getenv("RAG_MAX_DISTANCE", "0.85"))
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    documents = (result.get("documents") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    for text, metadata, distance in zip(documents, metadatas, distances):
        if distance is not None and float(distance) > max_distance:
            continue
        record_id = str(metadata.get("record_id", ""))
        unique_id = record_id or str(metadata.get("id", ""))
        if unique_id and unique_id in seen:
            continue
        if unique_id:
            seen.add(unique_id)
        hits.append({
            "id": record_id or str(metadata.get("id", "")),
            "type": str(metadata.get("category", "knowledge")),
            "title": str(metadata.get("title", "知识库片段")),
            "snippet": str(text)[:1200],
            "date": str(metadata.get("date", "")),
            "source": str(metadata.get("source", "本地知识库")),
            "url": str(metadata.get("url", "")),
            "data_quality": str(metadata.get("data_quality", "local")),
            "distance": round(float(distance), 4) if distance is not None else None,
        })
        if len(hits) >= limit:
            break
    return hits
