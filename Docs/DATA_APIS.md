# 数据接口

更新：2026-11-10。腾讯和东方财富的接口均为网站公开端点，匿名可访问，无需 Key。不是官方开放 API，字段和可用性可能变化，也没有使用条款承诺。

## 腾讯行情（`src/market.py`）

| 用途 | 端点 |
|---|---|
| 报价 | `https://qt.gtimg.cn/q=sh000001,sz300750` |
| 日线（前复权） | `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,320,qfq` |
| 证券搜索 | `https://smartbox.gtimg.cn/s3/?q=宁德&t=all` |

指数单位为点，股票为人民币元。成交量单位未核实，暂不使用。

## 东方财富事件（`src/events.py`）

| 用途 | 端点 / 报表 | 主要字段 |
|---|---|---|
| 公告列表 | `np-anotice-stock.eastmoney.com/api/security/ann` | 标题、日期、分类、`art_code` |
| 公告正文 | `np-cnotice-stock.eastmoney.com/api/content/ann?art_code=` | 正文文本、PDF 链接 |
| 解禁 | datacenter `RPT_LIFT_STAGE` | 解禁日、股数（万股）、占解禁前流通股比例、占总股本比例 |
| 股东增减持 | datacenter `RPT_SHARE_HOLDER_INCREASE` | 股东、方向、变动股数（万股）、占流通股比例、变动后持股比例 |
| 业绩预告 | datacenter `RPT_PUBLIC_OP_NEWPREDICT` | 预告指标、类型、金额区间、同比区间 |
| 财报主要指标 | datacenter `RPT_LICO_FN_CPD` | 营收、归母净利润（累计值）、同比、单季环比、毛利率、ROE、EPS |
| 主营构成 | `emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax` | 最近一期按产品、地区的收入占比、毛利率（上下游用） |
| 公告全文检索 | `search-api-web.eastmoney.com/search/jsonp` | 含“公司名 + 供货、客户、供应商…”的公告段落（上下游用） |
| 所属板块 | `emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax` | 一级、二级行业和精准概念板块（产业联动图用） |
| 板块涨跌 | `push2delay.eastmoney.com/api/qt/ulist.np/get` | 板块当日涨跌幅，百分数，缓存 60 秒 |

datacenter 地址：`https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=...&columns=ALL&filter=(SECURITY_CODE="300750")`。

单位处理：金额从元换算为亿元，比例统一为百分数，股数保留源单位万股，全部用 Decimal。

已知问题：

- 2026-10-26 在开发机上用 Python urllib 请求时，约一半响应在中途断开（`IncompleteRead`），curl 没有这个问题。同一天在海外 VPS 上不重试连续请求 5 次全部成功，问题应该出在开发机的代理环境。`_get_json` 仍保留一次重试。
- 公告全文检索常在握手或读取时超时，`search_mentions` 单独再重试一次；单次检索失败只跳过。
- 公告正文接口对部分公告（如 H 股报表）只返回 PDF，没有文本。
- 巨潮资讯 `hisAnnouncement/query` 当天测试返回空，未接入。

## Exa 新闻搜索（`src/news.py`）

`POST https://api.exa.ai/search`，请求头 `x-api-key`，Key 放在 `.env` 的 `EXA_API_KEY`。用于智能体工具 `search_news`（晨报、研究、脉络的新闻支线）、上下游的签约和订单新闻、每日行业新闻和宏观事件。

冒烟测试：`.venv/bin/python -m scripts.check_exa` 跑 5 个查询；`.venv/bin/python -m scripts.check_exa fast auto deep` 在 2 个查询上比较不同搜索类型的结果数、耗时和费用。类型名直接传给接口，名称不对时接口会报错。

2026-10-26 用 5 个近期真实事件测试（近 30 天、每次 5 条、带 300 字正文）：

- 25 条结果全是中文，日期都在范围内，内容和查询的事件对得上。
- 来源包括公司官网、财联社、第一财经、财新、东方财富、新浪财经、中国证券报、证券日报。
- 和东方财富数据交叉核对一致：宁德时代前三季度营收 4421.37 亿元、同比 56.19%；白银有色股东减持 300 万股；苏州固锝 9660 万股 10 月 27 日解禁。
- 能补上结构化数据没有的“原因”：例如宇树涨停的报道提到合作伙伴大会和行业数据。
- 问题一：同一公告被多家网站转载，白银有色 5 条基本是同一份公告。需要按标题去重、限制同一来源的条数。
- 问题二：混有行情页、AI 生成的“舆情汇总”和 SEO 站点，需要排除或标注来源等级。
- 问题三：`publishedDate` 偶尔比正文日期早一天。
- 问题四：泛泛的查询（如“贵州茅台 最新消息”）结果比较杂，查询词要带具体事件。
- 费用：每次搜索 0.007 美元，5 次共 0.035 美元。

注意：搜索词会包含持仓股票名称，相当于把持仓发给了 Exa。开发沙箱访问不了 api.exa.ai，这类测试需要在本机终端或 VPS 上运行。

## 模型

`POST {LLM_BASE_URL}/chat/completions`，OpenAI 兼容格式，按供应商 token 计费。当前使用 DeepSeek 官方（`https://api.deepseek.com`），主模型返回 402、429、5xx 或超时时切换到 `LLM_FALLBACK_*` 配置的备用模型（PLAN 5.1）。
