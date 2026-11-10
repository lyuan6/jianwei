# 见微

见微——金融事件脉络可视化智能体，第二届全国人工智能应用创新大赛通用赛道作品，名称取自“见微知著”。在线地址：https://jianwei.124701.xyz/ （需要评审账号登录）。

用户登录后有一份 A 股持仓。智能体每个交易日逐只检查持仓的公告、限售解禁、股东增减持、业绩预告和财报，按需读公告原文、查新闻、算走势，判断重要程度，生成晨报；再把每家公司一年来的公告串成一件件事项，跟踪公司说过的话后来有没有兑现。回答里的数字由程序回到工具结果里核对，核对不上标“未核对”；来源链接只用工具实际返回的地址；每一步记进工作底稿，可以展开核对。不给买卖、仓位或目标价建议，不预测涨跌。

## 功能

- **晨报：** 工作日 08:30 自动生成，按条款把事件分为重点、留意、一般，可回看往期。
- **脉络：** 一只股票的公告按事项串成地铁线路图，公告是站点，第三方新闻是支线；上游供应商、下游客户画在两翼。可切换到产业联动图（持仓暴露在哪些行业、近期发生了什么）和立体视图（每家公司一棵树，上方是宏观事件圆环）。
- **承诺台账：** 增持、回购、募投、业绩承诺等，状态随新公告更新，到期未披露由程序判断。
- **研究：** 就持仓自由提问，智能体自己决定查什么；脉络和承诺页也能直接打开分析。
- **持仓：** 增删改，搜索证券。

## 本地运行

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env                                   # AI 功能需填 APP_MODE=api、LLM_*、EXA_API_KEY
.venv/bin/python -m scripts.accounts add dev           # 建一个本地账号，密码只显示一次
COOKIE_SECURE=0 .venv/bin/python -m uvicorn src.web:create_app --factory --host 127.0.0.1 --port 8600
```

打开 http://127.0.0.1:8600 登录。直接用浏览器打开 `web/index.html` 不需要登录，看到的是示例数据。

## 测试和检查

```bash
.venv/bin/python -m unittest discover -s tests -q          # 网络和模型请求都用 mock
node --test tests/*.cjs                                    # 前端
python3 -m scripts.check_sources                           # 检查数据源能否从本机访问，只用标准库
.venv/bin/python -m scripts.check_exa                      # Exa 新闻搜索冒烟测试
.venv/bin/python -m scripts.evaluate                       # 评测，结果见 Docs/EVALUATION.md
.venv/bin/python -m scripts.backtest output/backtest.json  # 判断回测
```

## 技术

FastAPI + 标准库 sqlite3（WAL）+ 线程池后台任务；前端是原生 HTML/CSS/JS，没有构建步骤；智能体是自己写的工具调用循环（OpenAI 兼容格式），模型用 DeepSeek，主模型不可用时切换备用。运行时依赖只有 fastapi、uvicorn。数据来自腾讯行情、东方财富公开数据和 Exa 新闻搜索，见 [Docs/DATA_APIS.md](Docs/DATA_APIS.md)。

## 代码结构

| 路径 | 内容 |
|---|---|
| `src/web.py`、`src/accounts.py` | 接口、登录、会话、每日额度 |
| `src/storage.py`、`src/jobs.py` | 数据库读写、后台任务 |
| `src/model_client.py`、`src/agent.py`、`src/tools.py`、`src/prompts.py` | 模型请求、工具调用循环和工作底稿、工具注册表、提示词 |
| `src/brief.py`、`src/research.py` | 晨报、研究问答 |
| `src/timeline.py`、`src/commitments.py`、`src/chain.py` | 事件脉络、承诺台账、上下游 |
| `src/industry.py`、`src/macro.py` | 产业联动图、宏观事件 |
| `src/market.py`、`src/events.py`、`src/news.py` | 腾讯行情、东方财富事件、Exa 新闻 |
| `web/` | 前端；`timeline.js` 脉络，`industry.js` 产业图，`layers.js` 立体视图 |
| `scripts/` | 账号管理、每日任务、数据源检查、评测和回测 |
| `deploy/` | systemd 服务和每日定时 |
| `tests/` | 单元测试；`fixtures/eval_cases.json` 是 20 个评测案例 |

## 文档

- [Docs/PLAN.md](Docs/PLAN.md)：设计（产品、数据库、智能体、接口、前端）
- [Docs/EVALUATION.md](Docs/EVALUATION.md)：评测和判断回测结果，回测原始输出在 `Docs/backtest.json`
- [Docs/DATA_APIS.md](Docs/DATA_APIS.md)：数据接口和单位
- [Docs/DEPLOYMENT.md](Docs/DEPLOYMENT.md)：部署
- [Docs/PROGRESS.md](Docs/PROGRESS.md)：当前状态和已知不足
