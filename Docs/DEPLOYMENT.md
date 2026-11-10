# 部署

海外 VPS（SSH 别名 `oracle`，应用目录 `~/projects/ai-investment-research`）+ Cloudflare Tunnel。首次部署需要配置 `.env`、账号、systemd 和 Tunnel；后续更新见本文“更新”一节。

## VPS 首次部署

```bash
git clone <仓库地址> ~/projects/ai-investment-research && cd ~/projects/ai-investment-research
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # 用编辑器填 APP_MODE=api、LLM_*、EXA_API_KEY；值不加引号，行尾不写注释
python3 -m scripts.check_sources        # 确认数据源和 Key 都能用
.venv/bin/python -m scripts.accounts add reviewer1 reviewer2 reviewer3 reviewer4 reviewer5 team   # 密码只显示一次，马上保存
```

如果创建 venv 时报错，Debian/Ubuntu 先装 `python3-venv`。

先手动试运行，确认能访问：

```bash
.venv/bin/python -m uvicorn src.web:create_app --factory --host 127.0.0.1 --port 8600
curl -s http://127.0.0.1:8600/api/me   # 返回 {"detail":"请先登录。"} 就说明服务正常
```

然后交给 systemd 管理：

```bash
sudo cp deploy/jianwei.service /etc/systemd/system/   # 先把文件里的 USER 和路径改成实际的
sudo systemctl daemon-reload && sudo systemctl enable --now jianwei
journalctl -u jianwei -f
```

每日任务（工作日北京时间 08:30 生成晨报，再增量更新上下游、行业新闻和宏观事件）：

```bash
sudo cp deploy/jianwei-brief.service deploy/jianwei-brief.timer /etc/systemd/system/   # 同样先改 USER 和路径
sudo systemctl daemon-reload && sudo systemctl enable --now jianwei-brief.timer
journalctl -u jianwei-brief -n 30 --no-pager
```

Cloudflare Tunnel：在已有的 tunnel 里新增一条 Public Hostname，Service 填 `http://localhost:8600`。不要开 Cloudflare Access，否则评委要先过 Cloudflare 的验证才能看到登录页。服务只监听本机，VPS 不用对外开端口。

## 更新

```bash
cd ~/projects/ai-investment-research && git pull --ff-only && .venv/bin/python -m pip install -r requirements.txt && sudo systemctl restart jianwei
```

## 账号

```bash
.venv/bin/python -m scripts.accounts list
.venv/bin/python -m scripts.accounts reset reviewer1     # 重置密码，旧会话失效
.venv/bin/python -m scripts.accounts disable reviewer1   # 停用账号，比如一个评审阶段结束后
```

15 分钟内同一账号失败 5 次，或同一 IP 失败 20 次，就暂时不能登录。
