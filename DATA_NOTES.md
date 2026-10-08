# 数据字段、来源和处理说明

## 采集源

| 类别 | 默认 Provider | 内容 | 说明 |
|---|---|---|---|
| 矿业新闻 | Mining.com RSS (`https://www.mining.com/feed/`) 与 Google News mining RSS 兜底 | 标题、摘要、发布日期、链接 | Mining.com 在当前验证网络返回 403；聚合 RSS 作为独立实时新闻来源兜底 |
| 关键矿产政策 | Google News RSS 关键矿产政策检索；澳大利亚 DISR 关键矿产页面；中国政府网《稀土管理条例》 | 新近政策相关条目及两份官方背景资料 | 聚合结果须核对原文；官方页面地址分别为 [DISR](https://www.industry.gov.au/mining-oil-and-gas/minerals/critical-minerals) 和 [中国政府网](https://www.gov.cn/zhengce/content/202406/content_6960153.htm) |
| 市场价格 | Yahoo Finance 公共 chart endpoint | 配置的期货日线收盘价 | 默认 `HG=F,GC=F,SI=F`；需注意品种代码及计价单位由上游决定 |

可授权来源的 LME、SHFE CSV 可放入 `storage/imports/lme.csv` 和 `storage/imports/shfe.csv`，通过独立 Provider 导入。CSV 表头为 `date,commodity,value,unit,source,url`（`source`、`url` 可省略）；SHFE 官网提供数据下载与历史数据入口，见[官方数据下载](https://www.shfe.com.cn/reports/tradedata/datadownload/)。LME 市场数据须按其[数据许可条款](https://datalicensing.lme.com/agr/distribution)获取，不尝试绕过访问控制。

Yahoo Finance 公共行情代码不保证对应 LME/SHFE 官方结算价，默认可用公开 chart 数据只用于辅助演示；真实交易所报价以有权使用的交易所/授权供应商 CSV 为准，不可将不同币种或单位的价格直接比较。演示样例中的碳酸锂和铜走势是**合成数据**，只用于本地演示和测试，不是历史或实时市场事实。

## 统一记录字段

每条记录包含 `id`、`category`（`news` / `policy` / `price`）、`title`、`content`、`published_at`（ISO 日期）、`source`、`url` 和 `data_quality`。价格记录额外包含 `commodity`、数值 `value` 和带币种/计量单位的 `unit`。

## 规范化、去重与缓存

- 标题和正文压缩连续空白；发布时间规范为 ISO 日期；价格数值转成有限小数并保留 Provider 给出的单位。
- 新闻和政策有 URL 时按去除末尾斜线后的 URL 去重；价格按来源、商品、日期和单位区分，避免把不同交易所或计价单位的数据误合并。
- 每个 Provider 使用独立 JSON 缓存，临时文件写入后原子替换。Provider 失败时先读该 Provider 缓存；首次失败时使用带 `illustrative` 标志的离线演示数据。
- `storage/demo_data.json` 中的 120 条示例含 30 条新闻、30 条政策、30 条碳酸锂价格和 30 条铜价记录，标题或来源清楚标明虚构/合成。

## 评估定义

- **Recall@5**：每题预先标注相关记录 ID，计算返回 Top 5 中命中相关 ID 的比例，再对 20 题取平均。
- **Answer Faithfulness**：回答中的证据声明必须引用数据集内记录；标题声明与来源标题一致，价格区间声明重新按引用记录计算百分比并核对。该自动校验是确定性 provenance 检查，不代替人工语义审查。
- 评估固定使用离线基准集，并关闭可选 LLM，避免外部来源和模型输出让成绩不可重复。
