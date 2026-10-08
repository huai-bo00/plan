# 矿业情报问答 Demo

一个可离线演示的矿业情报 Web 应用，整理矿业新闻、关键矿产政策与市场价格，通过自然语言查询返回摘要、价格变化和来源。

## 功能

- FastAPI `/query` 问答接口和内置 Web 页面。
- Provider 数据源适配：Mining.com 与 Google News RSS、官方政策页面、Yahoo 公共行情、LME/SHFE 授权 CSV 导入。
- 独立来源缓存、字段规范化、来源内去重，并隔离不同价格来源和计量单位。
- 离线样例数据、20 条 ground truth、Recall@5 和证据一致性评估。
- 查询 `trace_id` 与阶段耗时日志；可选 OpenAI-compatible LLM，默认关闭。
- 基础知识问答、会话级最近对话记忆与清除入口；记忆保存在本地 SQLite。

## 快速开始

参见 [RUN.md](RUN.md)。数据字段、来源范围和评估口径见 [DATA_NOTES.md](DATA_NOTES.md)。

## 项目结构

```text
frontend/            页面、样式和浏览器端交互
backend/app/         FastAPI、Provider、LangGraph 问答和 SQLite 记忆模块
backend/pipeline/    采集、规范化和去重入口
backend/eval/        20 条问答和评估脚本
backend/tests/       API、数据管线和服务测试
storage/             演示数据及可选的授权 LME/SHFE CSV
```
