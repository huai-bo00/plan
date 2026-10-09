# 矿业情报问答 Demo 运行说明

## 环境

- Python 3.10 或更高版本
- Windows、macOS 或 Linux
- 新闻与政策实时采集需要网络；无网络时页面使用 `storage/demo_data.json` 的离线样例

## 安装与启动

```powershell
cd E:\myprojects\plan
py -3 -m venv .venv-demo
.\.venv-demo\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn serve.main:app --app-dir backend --reload
```

打开 http://127.0.0.1:8000 。API 文档位于 http://127.0.0.1:8000/docs ，健康检查为 http://127.0.0.1:8000/health 。

## 数据采集

启动 FastAPI 服务和提交问答都**不会自动爬取**。首次采集前页面使用 `storage/demo_data.json` 的离线样例。要抓取/刷新预先配置的新闻、政策和行情来源，可在首页“数据采集”卡片点击“开始采集”；页面会调用 `POST /ingest`，并显示各 Provider 的状态、记录数和耗时。采集完成后，后续问答会读取更新后的数据。

也可以在项目根目录用命令行手动刷新：

```powershell
python backend/pipeline/ingest.py
```

采集结果保存到 `storage/data.json`，Provider 缓存保存在 `storage/cache/`。也可通过 API 文档页面的 `POST /ingest` 触发刷新。某个来源失败时会使用该来源上次缓存；首次失败时使用对应的离线样例。

官方政策页默认使用轻量 `requests` 抓取；遇到 HTTP 错误或正文过短时自动尝试 Crawl4AI。Crawl4AI 是可选依赖，需要浏览器渲染兜底时额外安装：

```powershell
python -m pip install -r requirements-crawl4ai.txt
crawl4ai-setup
```

`POLICY_CRAWLER_ENGINE` 可设为 `hybrid`（默认）、`requests` 或 `crawl4ai`。`POLICY_CRAWL_MAX_PAGES`、`POLICY_CRAWL_MAX_DEPTH`、`POLICY_CRAWL_TIMEOUT` 和 `POLICY_CRAWL_MIN_TEXT` 控制同域采集范围及正文门槛。未安装或初始化 Crawl4AI 时，失败会继续走现有 Provider 缓存和样例兜底。

如有权使用交易所或授权供应商的历史行情 CSV，可分别保存为 `storage/imports/lme.csv`、`storage/imports/shfe.csv`，列名为 `date,commodity,value,unit`，可选 `source,url`。日期使用 `YYYY-MM-DD`，`value` 使用数值，`unit` 写明币种和计量单位。导入后运行 `python backend/pipeline/ingest.py`。不要把不同合约、来源或单位的数据直接拼成一条价格序列。

## 演示步骤

1. 在页面选择“锂价与政策”示例问题。
2. 展示最近 7 天价格变化、新闻和政策来源卡片。
3. 点开来源检查标题、日期及原始链接；离线合成数值会标记“演示样例”。
4. 查询一个不存在于当前数据中的主题，确认系统会说明资料不足。

## 评估与测试

```powershell
python backend/eval/evaluate.py
python -m pytest backend/tests
```

评估结果输出至 `backend/eval/results.json`，包含 20 个问题的 Recall@5、Answer Faithfulness 和逐题明细。

## 可选 LLM

默认不开启 LLM，问答仍可独立工作。复制 `.env.example` 为 `.env`，设置 `LLM_ENABLED=true`、OpenAI-compatible `LLM_BASE_URL`、`LLM_API_KEY` 和 `LLM_MODEL` 后启用。价格计算和数据检索不交给模型改写；LLM 请求失败时回退到规则答案。

也可在首页展开“模型配置”面板填写 API Key，保存后启用 LLM。默认使用 OpenAI API 和 `gpt-4o-mini`；如使用兼容服务，可在 `.env` 中调整 `LLM_BASE_URL` 与 `LLM_MODEL`。配置保存到项目根目录 `.env`，当前服务立即生效；页面不会回显已保存的 Key。不要将 `.env` 提交到版本控制。

## RAG 向量检索

矿种、矿业、新闻和政策等领域问题会经过独立的 LangGraph `rag_retrieve` 节点。普通常识问题会跳过 RAG，直接交给 LLM。RAG 使用本地 Chroma 持久化向量库，自动将新闻/政策记录与 `storage/knowledge/` 下的 `.md`、`.txt` 文档切分后索引；数据变化后在下一次领域查询时重建索引。来源信息会随检索片段返回。

向量化通过 OpenAI-compatible Embeddings API 完成，首页“向量化配置”可单独填写 Embedding API Key；未填写时回退复用模型 Key。默认模型为 `text-embedding-3-small`。所用服务商必须支持 embeddings 接口和该模型；如需调整，在 `.env` 设置 `RAG_EMBEDDING_BASE_URL`、`RAG_EMBEDDING_MODEL`。Key 单独保存在项目根目录 `.env`，页面不会回显。没有 Key、缺少 Chroma 或 embedding 请求失败时，RAG 节点会跳过/报状态并继续现有规则检索与 LLM 链路。索引文件保存在 `storage/chroma/`，不会提交到 Git。

## LangGraph 问答流程

问答由 LangGraph `StateGraph` 编排，按条件经过以下节点：

```text
parse_query → (rag_retrieve 可选) → retrieve_evidence → calculate_price → compose_answer → optional_llm → finalize
```

每个节点会记录耗时；响应的 `meta.graph_nodes` 列出实际流程节点，`meta.timings_ms` 包含逐节点和总耗时。数据采集属于独立的离线/手动刷新流程，不在每次问答时触发。

## 基础问答与对话记忆

不属于矿业情报检索的普通问题会自动绕过矿业资料检索，直接交给 LLM 回答；保存 API Key 后可使用，未配置时页面会提示启用模型。矿业新闻、政策与行情问题仍使用原有检索和计算流程。当前浏览器会话的最近 12 轮问答保存在 `storage/memory.sqlite3`，用于理解“它为什么上涨？”之类的追问。首页的“对话记忆”区域展示最近内容，“清除记忆”只清除此浏览器对应会话。数据库为本地文件，已加入 `.gitignore`。
