# Stock Research Agent

面向 A 股日线级投研的证据驱动多 Agent 系统。项目使用 LangGraph 编排研究流程，最终同时输出
结构化 JSON 和便于阅读的 Markdown 报告。

> 本项目仍处于早期版本，输出仅供研究和软件演示，不构成投资建议。

## 一、正式版目前能做什么

当前 `main` 分支实现的是“无辩论双经理输出”版本，执行一次完整的全市场每日研究：

```text
Tushare-compatible 行情/财务数据 + AKShare 新闻/公告
                         ↓
技术分析 / 情绪与资金 / 基本面 / 新闻事件 四位研究 Agent
                         ↓
证据汇总 → 候选观点生成 → 逐观点查证与补证
                         ↓
激进型基金经理 ─┐
                 ├─ 分别生成独立的正式投资建议
保守型基金经理 ─┘
                         ↓
结构化 ResearchReport + Markdown 报告
```

主要特点：

- 四位研究 Agent 从技术、资金情绪、基本面和新闻事件四个角度生成带来源的结构化证据；
- 首席研究策略师根据证据提出候选观点，查证 Agent 可继续调用受控 Tool 定向补充证据；
- 只有经过查证的观点可以直接支持投资建议；
- 激进型和保守型基金经理读取同一批终态观点，各自给出一份正式建议；
- 正式版不执行经理辩论，也不会虚构双方已经达成共识；
- 数据源失败、引用缺失和模型结构化输出错误会进入报告诊断信息。

当前命令行入口固定研究 `MARKET / A_SHARE / A股市场`。面向指定个股的 HTTP API、定时任务、数据库
持久化和断点恢复尚未接入正式版。

## 二、如何配置和运行

### 1. 环境要求

- Python 3.12 或更高版本；
- [uv](https://docs.astral.sh/uv/)；
- 能够访问所配置的大模型、行情数据源及 AKShare 使用的公开网页。

克隆仓库并安装依赖：

```bash
git clone https://github.com/RESTITUTOR-ORBIS-AURELIANUS/StockResearchAgent.git
cd StockResearchAgent
uv sync
```

### 2. 大模型支持范围

当前正式验证的模型是阿里云百炼提供的 **Qwen3.8-Max**，通过其 OpenAI-compatible 接口调用，并使用
原生严格 JSON Schema 约束 Agent 输出。

底层代码虽然采用 OpenAI-compatible SDK，但这不代表所有兼容接口或模型都已经可用。其他模型可能需要
调整 `LLM_STRUCTURED_OUTPUT_METHOD`、Schema strict 能力或模型适配逻辑，目前不属于正式支持范围。

### 3. 数据源说明

项目使用两类数据源：

1. **Tushare-compatible 数据服务**
   - 提供行情、指数、板块、资金流、财务报表、财务指标和卖方研究摘要等结构化数据；
   - 需要配置主服务器 API Key 和备用服务器 Token；
   - 程序总是优先访问主服务器，主服务器发生网络、HTTP、认证、权限、限流、业务或格式错误时回退到备用服务器。
2. **AKShare**
   - 用于读取公开新闻、快讯、个股新闻和公告索引；
   - 不需要 API Key，安装项目依赖时会自动安装；
   - 其数据来自公开网页，可能受到网站改版、网络状态和访问频率影响。

### 4. 配置 `.env`

复制配置模板：

```bash
cp .env.example .env
```

然后编辑项目根目录下的 `.env`：

```dotenv
# Tushare-compatible 主数据源
TUSHARE_PRIMARY_BASE_URL=http://your-primary-server/path/to/tushare
TUSHARE_PRIMARY_API_KEY=your-primary-api-key

# Tushare-compatible 备用数据源
TUSHARE_BACKUP_BASE_URL=https://your-backup-server
TUSHARE_BACKUP_TOKEN=your-backup-token
TUSHARE_REQUEST_TIMEOUT_SECONDS=30

# AKShare 不需要 Token；这里只配置超时和线程数
AKSHARE_REQUEST_TIMEOUT_SECONDS=30
AKSHARE_MAX_WORKERS=2

# 阿里云百炼 Qwen
LLM_BASE_URL=https://your-workspace-id.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=your-bailian-api-key
LLM_MODEL=qwen3.8-max
LLM_REQUEST_TIMEOUT_SECONDS=1200
LLM_MAX_RETRIES=2
LLM_TEMPERATURE=0

# Qwen3.8-Max 当前使用的严格结构化输出配置
LLM_STRUCTURED_OUTPUT_METHOD=json_schema
LLM_STRUCTURED_OUTPUT_STRICT=true
LLM_STRUCTURED_OUTPUT_REPAIR_ATTEMPTS=1
LLM_STRUCTURED_OUTPUT_DIAGNOSTICS_PATH=.artifacts/llm-structured-output.jsonl
LLM_STRUCTURED_OUTPUT_RAW_MAX_CHARACTERS=20000
```

请使用阿里云控制台为自己提供的实际 compatible-mode 地址；上面的 workspace 地址只是格式示例。
`.env`、`.artifacts`、`.venv` 和 `.idea` 均已加入 `.gitignore`，不要使用 `git add -f` 强制提交密钥。

### 5. 执行每日研究

`--as-of` 是数据截止时间，必须是带时区的 ISO 8601 时间：

```bash
uv run stock-research-agent \
  --as-of 2026-08-27T15:30:00+08:00 \
  --format markdown \
  --output report.md
```

输出结构化 JSON：

```bash
uv run stock-research-agent \
  --as-of 2026-08-27T15:30:00+08:00 \
  --format json \
  --output report.json
```

不提供 `--output` 时，报告会直接输出到终端。退出码含义：

- `0`：生成了完整报告；
- `3`：生成了报告，但研究链不完整或记录了上游错误；
- `1`：运行失败，未能生成合法报告；
- `130`：用户中断运行。

结构化模型调用的耗时、token、`finish_reason`、校验错误和脱敏响应会写入
`.artifacts/llm-structured-output.jsonl`，可用于排查超时或 Schema 错误。

### 6. 运行测试

```bash
uv run pytest
uv run ruff check .
```

详细的架构、数据接口、Tool 和 Agent 契约见 [`docs/README.md`](docs/README.md)。

## 三、开发版正在做什么

`develop` 分支保留并继续完善“两位基金经理辩论”系统。它在两份独立建议之后增加：

```text
两位经理独立建议
        ↓
建议条目规范化与互斥关系识别
        ↓
双方逐条交叉评分 + 确定性冲突规则校验
        ↓
理由交换 → 原提议方修订 → 必要时重新评分（最多三轮）
        ↓
达成共识的建议进入委员会结果
未达成共识的建议标记为 EXCLUDED
```

开发版的目标不是强迫两位经理形成单一答案，而是保留双方原始意见，并让最终共识建议具有可追溯的
评分、理由、修订和排除记录。当前这套辩论链已经有实现代码和确定性测试，但仍在调整冲突识别、评分
约束、模型稳定性和真实运行成本，因此暂未作为 `main` 的默认运行路径。

相关设计文档：

- [`docs/cross-review-nodes-v1.md`](docs/cross-review-nodes-v1.md)
- [`docs/conflict-score-validator-v1.md`](docs/conflict-score-validator-v1.md)
- [`docs/formal-negotiation-nodes-v1.md`](docs/formal-negotiation-nodes-v1.md)
- [`docs/consensus-recommendation-assembler-v1.md`](docs/consensus-recommendation-assembler-v1.md)
