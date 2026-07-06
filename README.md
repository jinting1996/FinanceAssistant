# 盯盘侠 FinanceAssistant

自托管 AI 盯盘助手，覆盖 A 股 / 港股 / 美股，集成 TradingAgents 多 Agent 投资决策、事件日历、板块轮动、X 舆论监控与全渠道推送。

[![GitHub stars](https://img.shields.io/github/stars/PotatoChipking/finance?style=flat&logo=github&color=yellow)](https://github.com/PotatoChipking/finance/stargazers)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-wwkisu%2Ffinanceassistant-blue?logo=docker)](https://hub.docker.com/r/wwkisu/financeassistant)

![Dashboard](docs/screenshots/dashboard.png)

| 持仓管理 | AI 建议 |
|:---:|:---:|
| ![Portfolio](./docs/screenshots/portfolio.png) | ![Suggestion](./docs/screenshots/suggestion.png) |

| 事件日历与板块池 |
|:---:|
| ![Market Events](./docs/screenshots/market-events.jpg) |

<details>
<summary>移动端截图</summary>
<img src="./docs/screenshots/mobile.png" width="375" />
</details>

> 如果盯盘侠对你有帮助，点右上角 Star 支持一下 —— 这是对开源项目最好的鼓励。

## 目录

- [为什么选择盯盘侠](#为什么选择盯盘侠)
- [核心能力](#核心能力)
- [快速开始](#快速开始)
- [Docker 部署](#docker-部署)
- [配置指南](#配置指南)
- [本地开发](#本地开发)
- [技术栈](#技术栈)
- [贡献](#贡献)
- [License](#license)

## 为什么选择盯盘侠

- **数据私有** — 自托管部署，持仓数据不经过任何第三方
- **AI 原生** — 不是简单的指标堆砌，而是让 AI 理解你的持仓、风格和目标，输出可执行的建议
- **事件驱动** — 重大消息、板块日 K、MACD/RSI 和资金轮动放在同一个页面判断，减少信息跳转
- **开箱即用** — 本地一键启动，5 分钟完成配置；Docker 一条命令拉起
- **个人维护** — 本仓库 Fork 自 [PotatoChipking/FinanceAssistant](https://github.com/PotatoChipking/FinanceAssistant)，在上游基础上增加了 Docker Hub 镜像、X 舆论监控、CI/CD 自动构建等功能

## 核心能力

### 智能 Agent 系统

| Agent | 触发时机 | 功能 |
|-------|---------|------|
| 盘前分析 | 每日开盘前 | 综合隔夜美股、新闻消息、技术形态，给出今日操作策略 |
| 盘中监测 | 交易时段实时 | 监控异动信号，RSI/KDJ/MACD 共振时推送提醒 |
| 盘后日报 | 每日收盘后 | 复盘当日走势，分析资金流向，规划次日操作 |
| 新闻速递 | 定时采集 | 抓取财经新闻，AI 筛选与持仓相关的重要信息 |

### TradingAgents 深度分析

接入 [TradingAgents](https://github.com/TauricResearch/TradingAgents)（76k+ star）多 Agent 投资决策框架。在持仓页或事件日历的板块龙头股上点 🧠 图标即可触发，3-5 分钟输出完整推理链：

分析师团队（技术面 / 情绪面 / 新闻面 / 基本面）→ 多空辩论 → 风控审查 → PM 整合决策

结论同步推送到 Telegram / 企业微信 / 钉钉 / 飞书。支持未加入自选池的板块龙头股临时分析，适合从事件和板块轮动里快速下钻。默认 deepseek-chat，单次约 $0.05。

配置指南：[`.docs/tradingagents/USER_GUIDE.md`](.docs/tradingagents/USER_GUIDE.md)

### 事件日历与板块轮动

`/events` 页面将消息面事件和资金去向整合在同一个工作台：

- 按本周 / 本月 / 近 30 天聚合关注池新闻和公告，标注影响等级、情绪方向、关联板块和短线预判
- 结合热门行业板块涨跌幅、成交额、龙头股表现，识别资金聚焦、活跃轮动、降温退潮等状态
- 搜索并关注最多 8 个 A 股行业板块，刷新最近 120 个交易日真实板块指数日 K
- 自动计算板块 MA、MACD、RSI、近 1/5/20 日涨跌幅、趋势评分和轮动状态
- 板块卡片内展示强势成分股，可一键触发 TradingAgents 深度分析

### 专业技术分析

- **趋势指标**：MA 多空排列、MACD 金叉死叉、布林带突破
- **动量指标**：RSI 超买超卖、KDJ 钝化与背离
- **量价分析**：量比异动、缩量回调、放量突破
- **形态识别**：锤子线、吞没形态、十字星等 K 线形态
- **支撑压力**：自动计算多级支撑位和压力位

### Price Action 机会策略

基于前复权日 K 计算突破、回踩确认、趋势结构、ATR 风控和 PA 评分。在「更多 → 机会」点击刷新后，PA 会参与候选评分；可使用「Price Action」策略筛选查看结果。

候选自动生成入场区间、突破位、支撑位、止损位、目标位和结构失效条件。日 K 自动展示 PA 支撑/压力、止损/目标横线，以及历史突破和回踩确认标记。支持 PA 评分、PA 突破、PA 回踩确认和 PA 结构失效条件提醒，按信号日期去重。在「策略」页可启停 Price Action，并调整风险等级和策略权重。

PA 明细接口：

```http
GET /api/klines/{symbol}/price-action?market=CN&days=180
```

### 底仓 VWAP 回归做 T

仅扫描已有 A 股持仓，需在持仓编辑中维护可卖底仓和账户可用资金。日线趋势未破、接近支撑、低于当日 VWAP、分钟 K 止跌且 T Score 达到 70 时才触发提醒。收到低吸提醒后需在持仓页确认买入，系统才会继续监控 VWAP / 目标位卖点。

单次默认使用可卖底仓 20%，按 100 股取整，每股每天最多完成一次，跌破止损或信号超时自动失效。只生成提醒，不连接券商、不自动下单。A 股 / 港股 1 分钟数据使用腾讯行情，5 分钟 K 由后端按交易时段聚合。

### 价格提醒

支持价格、涨跌幅、成交额、量比、PA 评分/突破/回踩/结构失效等条件组合（AND / OR）。支持交易时段/全天生效、冷却时间、日触发上限、重复触发模式，到期时间留空表示永不过期。可按规则选择通知渠道，不选则走系统默认渠道。

### X 舆论监控

基于 `twikit` 免费抓取 X (Twitter) 上的 Cashtag（`$SYMBOL`）讨论，通过 AI 进行正面/负面/中性情感分类。分析结果汇总为结构化舆情数据，可作为 Agent 的补充数据源。内置速率限制（50 次/15 分钟）和 Cookie 持久化会话恢复。需要配置 X 账号（建议使用小号），在 `.env` 中设置 `X_USERNAME` / `X_EMAIL` / `X_PASSWORD` 即可启用。

### 多市场多账户

覆盖 A 股、港股、美股实时行情，支持多券商账户独立管理、汇总展示总资产，按短线/波段/长线分别设置交易风格，AI 建议更精准。

### 全渠道通知

Telegram / 企业微信 / 钉钉 / 飞书 / Bark / 自定义 Webhook，支持按 Agent 和规则分别选择通知渠道。

### 数据源

内置多路数据源，自动主备切换：

| 类型 | 数据源 | 说明 |
|------|--------|------|
| 新闻 | 雪球资讯、东方财富资讯、东方财富公告 | 雪球需 cookie |
| K 线 | 腾讯、东方财富、Stooq、Tushare、YFinance | 多源备选 |
| 资金流向 | 东方财富资金流 | 默认启用 |
| 行情 | 腾讯行情 | 默认启用 |
| 事件 | 东方财富事件 | 默认启用 |
| 社交 | X/Twitter（Cashtag 搜索） | 可选配置 |

## 快速开始

```bash
git clone https://github.com/PotatoChipking/finance.git
cd finance
make dev-api        # 启动后端（自动 venv + 依赖，监听 :8000）
make dev-web        # 启动前端（自动 pnpm install，监听 :5183）
```

访问 `http://localhost:5183`，首次使用设置账号密码后即可进入。前端 dev server 用 `:5183` 而非默认 `:5173`，是为了和 BeeCount-Cloud 等本地常驻前端错开。

## Docker 部署

### 从源码构建

仓库自带的 `docker-compose.yml` 从当前源码构建镜像，不依赖任何外部镜像：

```bash
git clone https://github.com/PotatoChipking/finance.git
cd finance
cp .env.example .env            # 按需填写 AI_API_KEY 等
docker compose up -d --build    # 构建镜像并后台启动
```

浏览器打开 `http://<服务器IP>:8000`。数据（SQLite、Playwright 浏览器、运行时文件）持久化在 `FinanceAssistant_data` 卷中，升级不丢失。

```bash
docker compose logs -f                     # 查看日志
git pull && docker compose up -d --build   # 升级：拉代码后重建
docker compose down                        # 停止
```

### 从 Docker Hub 拉取预构建镜像

CI/CD 自动构建并推送到两个 Registry，适合国内网络环境或 NAS 设备直接拉取：

```bash
# Docker Hub（国内友好）
docker pull wwkisu/financeassistant:latest

# GitHub Container Registry
docker pull ghcr.io/jinting1996/financeassistant:latest
```

镜像标签：`latest` / `main` / `sha-<短哈希>`，每次 push 到 main 或每日凌晨 2:00 自动构建。

### NAS 部署

绿联等 NAS 设备可直接使用预构建镜像。将 `docker-compose.yml` 中的 `image: FinanceAssistant:local` 替换为 `wwkisu/financeassistant:latest`，删除 `build:` 段，按需修改端口和数据卷路径即可。

## 配置指南

### 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `AUTH_USERNAME` | 预设登录用户名 | 首次访问时设置 |
| `AUTH_PASSWORD` | 预设登录密码 | 首次访问时设置 |
| `JWT_SECRET` | JWT 签名密钥（多容器/重建时建议固定） | 自动生成 |
| `AI_BASE_URL` | AI API 地址 | `https://open.bigmodel.cn/api/paas/v4` |
| `AI_API_KEY` | AI API 密钥 | 空 |
| `AI_MODEL` | AI 模型名称 | `glm-4` |
| `DATA_DIR` | 数据存储目录 | `./data` |
| `TZ` | 应用时区（影响调度与时间展示） | `Asia/Shanghai` |
| `DAILY_REPORT_CRON` | 日报调度 cron | `30 15 * * 1-5` |
| `HTTP_PROXY` | 出站 HTTP 代理 | 未设置 |
| `CA_CERT_FILE` | SSL 企业证书路径（Zscaler 等环境） | 未设置 |
| `PLAYWRIGHT_SKIP_BROWSER_INSTALL` | 跳过 Chromium 安装（不需要截图时） | 未设置 |
| `UPDATE_CHECK_DOCKER_REPO` | 升级检测的镜像仓库，留空不检测 | 未设置 |
| `LOG_LEVEL` | 控制台日志级别（`INFO` / `DEBUG`） | `INFO` |
| `X_USERNAME` | X 账号用户名（舆论监控） | 空 |
| `X_EMAIL` | X 账号邮箱（舆论监控） | 空 |
| `X_PASSWORD` | X 账号密码（舆论监控） | 空 |
| `SOCIAL_SENTIMENT_ENABLED` | 启用 AI 情感分析 | `false` |

支持 OpenAI / 智谱 / DeepSeek / Ollama 等所有 OpenAI 兼容 API。

### 首次配置

1. 访问 Web 界面，设置登录账号
2. **设置 → AI 服务商**：配置 API 地址和密钥
3. **设置 → 通知渠道**：添加 Telegram 或其他推送渠道
4. **持仓 → 添加股票**：添加自选股，启用对应 Agent

### WSL 部署代理说明

WSL 中部署时，`127.0.0.1` 指向的是 WSL 自身而非宿主机。需要代理的数据源可能因此连接失败。在 WSL 中查看宿主机地址：

```bash
cat /etc/resolv.conf | grep nameserver
```

假设输出 `nameserver 172.24.112.1`，则代理配置为：

```bash
export HTTP_PROXY=http://172.24.112.1:8897
export HTTPS_PROXY=http://172.24.112.1:8897
```

持仓页分时图和 5 分钟 K 线使用腾讯行情，通常不需要为东方财富分钟接口单独配置代理。可在 WSL 内验证腾讯 A 股分钟 K 线是否能访问：

```bash
curl 'https://web.ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh600519,m1,,20'
```

返回内容中包含 `data` 和 `m1` 说明腾讯分钟线可用。

## 本地开发

**环境要求**：Python 3.10+ / Node.js 18+ / pnpm

```bash
# 一键开发（推荐）
make dev-api          # 后端 :8000（自动 venv + 依赖）
make dev-web          # 前端 :5183（自动 pnpm install）

# 或手动
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python server.py
cd frontend && pnpm install && pnpm dev
```

前端 dev server 运行在 `http://localhost:5183`，`/api` 代理到 `127.0.0.1:8000`。

## 技术栈

| 层 | 技术 |
|----|------|
| 后端框架 | FastAPI |
| 数据库 | SQLite（SQLAlchemy ORM） |
| 任务调度 | APScheduler |
| AI 客户端 | OpenAI SDK（兼容多厂商） |
| 深度分析 | TradingAgents（多 Agent 投资决策） |
| 前端框架 | React 18 + TypeScript |
| 样式 | Tailwind CSS + shadcn/ui |
| 浏览器自动化 | Playwright（K 线截图） |
| 容器化 | Docker 多阶段构建 |
| CI/CD | GitHub Actions（自动构建 + 多仓推送） |

## 运行时说明

后端推荐通过 `python server.py` 启动，它会初始化数据库迁移、认证、日志、调度器等全部运行时服务。直接用 `uvicorn src.web.app:app` 启动也受支持，因为 `src.web.app` 会委托到相同的生命周期初始化路径，两种入口的迁移、认证、调度器行为一致。

上游仓库地址：`https://github.com/PotatoChipking/finance.git`

## 贡献

本仓库由 [PotatoChipking](https://github.com/PotatoChipking) 维护。自定义 Agent 和数据源开发请参考[贡献指南](CONTRIBUTING.md)。

## License

[MIT](LICENSE)