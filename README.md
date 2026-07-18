<p align="center">
  <b>简体中文</b> · <a href="./README.en.md">English</a>
</p>

<h1 align="center">Open Claude Tag Lark</h1>

<p align="center">
  <b>飞书群里的 AI 数字员工团队</b> —— 一个群 = 一支团队：产品、研发、调度各司其职，<br/>
  你一句话，它们自己拆任务、排队执行、交付文档。共享记忆，永不忘事。
</p>

<p align="center">
  <a href="https://github.com/jiangdaxia-AI/open_claude_tag_lark/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/jiangdaxia-AI/open_claude_tag_lark?style=social" /></a>
  <a href="https://github.com/jiangdaxia-AI/open_claude_tag_lark/forks"><img alt="GitHub forks" src="https://img.shields.io/github/forks/jiangdaxia-AI/open_claude_tag_lark?style=social" /></a>
  <a href="https://github.com/jiangdaxia-AI/open_claude_tag_lark/issues"><img alt="GitHub issues" src="https://img.shields.io/github/issues/jiangdaxia-AI/open_claude_tag_lark" /></a>
  <a href="https://github.com/jiangdaxia-AI/open_claude_tag_lark/commits/main"><img alt="last commit" src="https://img.shields.io/github/last-commit/jiangdaxia-AI/open_claude_tag_lark" /></a>
</p>

<p align="center">
  <a href="./LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-blue.svg?style=flat" /></a>
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-blue?style=flat&logo=python&logoColor=white" />
  <img alt="feishu" src="https://img.shields.io/badge/Feishu-native-3370ff?style=flat" />
  <img alt="llm-agnostic" src="https://img.shields.io/badge/LLM-agnostic-6366f1?style=flat" />
  <img alt="mcp-native" src="https://img.shields.io/badge/MCP-native-10b981?style=flat" />
</p>

<p align="center">
  <code>agent-memory</code> · <code>feishu-bot</code> · <code>multi-agent</code> · <code>ai-agents</code> · <code>mcp</code> · <code>llm</code> · <code>claude</code> · <code>lark</code> · <code>self-hosted</code> · <code>python</code>
</p>

<p align="center">
  <i>如果这个项目对你有帮助，请点个 ⭐ Star 支持一下 —— 这是我们持续迭代的最大动力</i>
</p>

<!-- 🎬 DEMO GIF：录好后取消下面这段的注释，并把文件放到 assets/demo.gif。
     录制内容（20-30 秒，最能打动人的流程）：
     群里发一句"我要做个XX小程序，出个需求文档，让研发review一下"
     → 主 agent 规划任务 → @产品 agent 写 PRD → @代码 agent 技术评审 → 任务看板实时更新。
     录制工具推荐：Kap (Mac) / LICEcap / ScreenToGif，控制在 5MB 以内。

<p align="center">
  <img src="assets/demo.gif" alt="多 Agent 协作演示" width="720" />
</p>
-->

---

## 这是什么

[Anthropic Claude Tag](https://www.anthropic.com/news/introducing-claude-tag) 提出了一个革命性理念：AI 不应该是私聊里的个人助手，而应该是**频道里的共享队友**——一个群一个 agent，所有人共享同一份记忆，agent 知道谁说了什么、记得上次的结论、能主动跟进没做完的事。

但 Claude Tag 是闭源、付费、锁定 Anthropic 模型、只在 Slack 里可用。

**Open Claude Tag Lark 是它的开源复刻，落地到飞书生态**：

- **数字员工，不是聊天机器人** — 每个 agent 是一个有身份、有记忆、有专长的"数字员工"，像真同事一样在群里协作
- **多员工自动分工** — 主 agent 把子任务 `@委派` 给专门 agent（产品专家、代码专家...），运行时自动建依赖链、按序唤醒、任务追踪，各司其职
- **隔离沙箱执行代码** — 内置 OpenSandbox 代码执行环境，agent 可以在沙箱里跑 Python/JS/Go、读写文件、装依赖，session 内状态持久
- **三层记忆，越用越聪明** — 全局知识 / 任务进展 / 当前对话分层存储，向量检索按需召回，记忆再多也不拖慢上下文
- **飞书生态完美集成** — 基于 lark-oapi 长连接、互动卡片流式输出、多 bot 身份、@用户名自动解析，原生体验无割裂感
- **完全可扩展** — LLM 无关（一行配置切 Claude/GPT/DeepSeek/Ollama）、MCP 工具生态（任意 MCP server 即插即用）、文件化配置（git 可管理）、自托管（数据完全在你手里）

市面上的飞书 AI bot 几乎都是"个人助手"——你私聊它，它只记得你说过的话，群里其他人完全不知道。换个同事去问，它又从零开始。Open Claude Tag Lark 反过来做：**一个飞书群 = 一个共享数字员工**，群里所有人共用同一份记忆，新人进群也能立刻接上下文，不用再翻聊天记录。

## 产品截图

Web 控制台（`WEB_ADMIN_ENABLED=true` 启动后访问 `:8765`）：

<table>
  <tr>
    <td width="50%">
      <img src="assets/console-overview.png" alt="概览" /><br />
      <b>概览</b> — 频道、Agent、任务状态一目了然
    </td>
    <td width="50%">
      <img src="assets/console-kanban-cn.png" alt="任务看板" /><br />
      <b>任务看板</b> — 多 Agent 任务按状态分列，进度实时可见
    </td>
  </tr>
  <tr>
    <td width="50%">
      <img src="assets/console-ledger.png" alt="执行记录" /><br />
      <b>执行记录</b> — 每个 Agent 每次执行的工具链、耗时、输出全程可溯
    </td>
    <td width="50%">
      <img src="assets/console-files.png" alt="工作区文件" /><br />
      <b>工作区文件</b> — Agent 产出的 PRD、技术评审等文档集中管理
    </td>
  </tr>
</table>

## 它能为你做什么

| 场景 | 以前 | 用 Open Claude Tag Lark |
|---|---|---|
| **团队知识不丢失** | 关键决策散在几百条消息里，新人入职只能靠口口相传 | Agent 自动把重要结论写进 `MEMORY.md`，下次有人问同样的事直接答 |
| **多人接力不卡壳** | A 问了问题下班了，B 第二天想接着问，bot 早忘了 | 同一群里所有人共享上下文，B 可以直接续问 |
| **复杂任务自动分工** | 一个 agent 干所有事，常常答非所问 | 主 agent 把子任务 `@委派` 给专门 agent（如代码审查、文档总结），各司其职 |
| **不会"聊着聊着就忘了"** | 普通 bot 上下文一长就开始胡说 | Agent 用内循环策展记忆，主动决定什么值得长期记、什么可以忘 |
| **主动跟进未完成的事** | 群里提问没人答，事情就石沉大海 | 心跳巡检发现 48h 没回复的线程，agent 主动 @ 相关人跟进 |
| **能力越用越强** | 每次都从零开始，没有积累 | 复杂任务做完后自动写 `SKILL.md`，下次类似任务直接复用经验 |
| **数据完全在自己手里** | SaaS AI bot 的对话数据存在别人服务器 | 自托管，所有记忆、对话、工作区文件都在你的服务器上 |
| **不锁定任何模型** | 用了某家 bot 就只能用它家的模型 | 一行配置切换 Claude / GPT / DeepSeek / Gemini / 本地 Ollama，不同群还能用不同模型 |

## 30 秒看懂

```
[#工程群]

@Alice   这个 auth 重构的 PR 谁能帮忙看下？
@Open Claude Tag Lark  我拉了一下 PR，整体没问题，一个建议：
          第 42 行的 session 过期没处理时钟偏移。
          @Bob 你上周 DB 迁移提过这个模式——这里也要加吗？
@Bob     对，加 5s leeway，跟 auth/session.py:L88 一样
@Open Claude Tag Lark  收到。已记到 MEMORY.md：
          "session 过期：始终加 5s 时钟偏移容差（auth/session.py:L88）"
```

群里所有人看到同一条线程。Agent 知道谁说了什么、主动 @ 对的人跟进、自己决定什么值得长期记。

## 为什么不一样

| | 普通 DM 助手 | Claude Tag (Anthropic) | **Open Claude Tag Lark** |
|---|---|---|---|
| 平台 | 各平台都有 | 仅 Slack | **飞书原生** |
| 上下文归属 | 每人一份，互相隔离 | 一群一份，全员共享 | **一群一份，全员共享** |
| 多员工协作 | 不支持 | 单 agent | **主 agent 委派子任务给专门 agent** |
| 长期记忆 | dumb append-only，越攒越乱 | agent 自策展 | **Agent 自策展，主动决定记什么、忘什么** |
| 能力积累 | 没有 | 有 | **复杂任务后自动生成技能，下次复用** |
| 主动行为 | 只能被动应答 | 有心跳巡检 | **心跳巡检，主动跟进未完成的事** |
| 模型 | 用哪家锁哪家 | 锁定 Claude | **LiteLLM 统一接口，一行配置切换** |
| 工具扩展 | 厂家给什么用什么 | MCP 但需审批 | **任意 MCP server 即插即用，按频道配置** |
| 数据归属 | SaaS 服务器 | Anthropic 云 | **自托管，完全在你手里** |
| 开源 | 否 | 闭源 | **MIT 开源** |

---

## 快速开始

### 前置条件

- Python 3.11+
- 一个飞书自建应用（[open.feishu.cn/app](https://open.feishu.cn/app)）
- 一个 LLM 提供商的 API key

### 1. 创建飞书应用

1. 进入 [open.feishu.cn/app](https://open.feishu.cn/app) → **创建企业自建应用**
2. **凭证与基础信息**：记录 `App ID`、`App Secret`、`租户 ID`
3. **事件与回调** → **事件配置**：选择 **长连接（WebSocket）模式**（无需公网回调地址）
4. **事件订阅**：订阅 `im.message.receive_v1`（接收消息）
5. **权限管理**：授予 `im:message`、`im:message:send_as_bot`、`im:chat`、`im:resource`、`contact:user.id:readonly`
6. **机器人**：启用机器人能力，把 bot 拉进目标群

### 2. 安装与配置

```bash
git clone <this-repo>
cd Claude-tag

# 推荐用 uv（项目已带 uv.lock）
uv sync
# 或 pip
pip install -e ".[dev,admin]"

cp .env.example .env
```

编辑 `.env`：

```bash
# 飞书应用凭证
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_TENANT_ID=xxxxxxxxxxxxxxxx
# 留空则启动时通过 app_id + app_secret 自动拉取
FEISHU_BOT_OPEN_ID=

# LLM 提供商（任选其一）
# Anthropic Claude（默认）
LLM_MODEL=claude-sonnet-4-6
ANTHROPIC_API_KEY=sk-ant-...

# OpenAI
# LLM_MODEL=gpt-4o
# OPENAI_API_KEY=sk-...

# OpenAI 兼容自建网关（如 gray-ai-gateway）
# LLM_MODEL=openai/qwen3.5-flash
# LLM_API_BASE=https://your-gateway/v1
# LLM_API_KEY=...

# 本地 Ollama
# LLM_MODEL=ollama/llama3

# 存储目录
DATA_DIR=./data

# 分层记忆（向量检索，推荐开启）
MEMORY_LAYERED_ENABLED=true
MEMORY_EMBEDDER_API_BASE=https://api.siliconflow.cn/v1   # 或任何 OpenAI 兼容 embedding 端点
MEMORY_EMBEDDER_MODEL=BAAI/bge-m3
MEMORY_EMBEDDER_API_KEY=sk-...

# 代码执行沙箱（可选，需 Docker + OpenSandbox Server）
SANDBOX_ENABLED=false
SANDBOX_DOMAIN=127.0.0.1:8079
SANDBOX_IMAGE=opensandbox/code-interpreter:v1.1.0
```

### 3. 配置频道

获取群 chat_id：在飞书群设置 → 群信息 → 复制群标识。

```bash
mkdir -p data/channels/oc_xxxxxxxxxxxxxxxx
cp channels/example/CHANNEL.md data/channels/oc_xxxxxxxxxxxxxxxx/
cp channels/example/tools.toml  data/channels/oc_xxxxxxxxxxxxxxxx/
cp channels/example/agents.toml data/channels/oc_xxxxxxxxxxxxxxxx/  # 多 agent 才需要
```

编辑 `CHANNEL.md` 描述这个群是干什么的：

```markdown
# 工程频道

你是工程团队的 AI 队友。

## 职责
协助部署、代码评审、故障响应、架构决策。

## 风格
技术、直接、简洁。多用代码块。大动作前先确认。
```

### 4. 启动

```bash
open-claude-tag-lark
```

然后在飞书群里 `@bot` 提问即可。

### 健康检查

```bash
open-claude-tag-lark doctor
```

会校验飞书凭证、LLM 连通性、SQLite 写入、Mem0 配置等。

### Web 控制台

```bash
# 启用 web admin（需要 fastapi + uvicorn）
WEB_ADMIN_ENABLED=true WEB_ADMIN_PORT=8765 open-claude-tag-lark
```

或独立启动：

```bash
python -c "from ocl.web_admin import app; import uvicorn; uvicorn.run(app, host='127.0.0.1', port=8000)"
```

访问 `http://127.0.0.1:8000/`，可看到：
- **概览** — 频道、Agent、任务、活跃状态
- **看板** — 任务按状态分列拖拽
- **账本** — 每次 agent 执行的工具链、耗时、最终输出
- **文件** — Agent 工作区文件浏览
- **诊断** — 一键健康检查

## 频道配置

每个频道是一个目录，全部是可读可版本管理的 Markdown / TOML：

```
data/channels/<chat_id>/
  CHANNEL.md       ← 频道身份、职责、风格
  MEMORY.md        ← agent 自维护的事实（自动更新，勿手改）
  tools.toml       ← MCP server 接入 + 频道级 LLM 覆盖
  agents.toml      ← 多 agent 注册（可选）
  skills/          ← agent 自动生成的技能文件
    deploy-to-staging.md
    oncall-handoff.md
  workspace/       ← agent 工作区，存放产物
```

### `agents.toml` — 多 Agent 注册

```toml
[[agent]]
id = "main"
display_name = "总指挥"
model = "claude-sonnet-4-6"     # 覆盖全局模型
scopes = ["web_search", "exec_code", "task_create"]  # 工具白名单

[[agent]]
id = "code-reviewer"
display_name = "代码审查员"
bot_app_id = "cli_yyyyy"         # 绑定独立飞书 bot
bot_app_secret = "yyyy"
model = "deepseek/deepseek-coder"
scopes = ["web_search", "save_artifact"]
```

主 agent 通过 `delegate` 工具把子任务委派出去，被委派的 agent 在群里以自己 bot 身份回复。

### `tools.toml` — 工具与模型覆盖

```toml
[llm]
model = "gpt-4o"                 # 本频道用不同模型

[[mcp_server]]
name = "github"
url = "mcp://localhost:3001"
allowed_tools = ["list_prs", "get_file", "create_comment"]
```

## 工作原理

```
飞书群 @bot
     │
     ▼
  WebSocket 接收（lark_oapi）
     │
     ▼
  Channel Router ──── chat_id → AgentSession
     │                  ↑ 串行锁：同一频道不并发写上下文
     ▼
  Context Assembler
  ├── CHANNEL.md       （身份、职责）
  ├── AGENT.md         （agent 人设 + 职责边界）
  ├── 分层记忆检索      （全局 + 任务记忆，embedding 按需召回）
  ├── skills/*.md      （语义匹配后按需加载）
  └── 最近 N 条消息     （按 agent 归属：自己的话 vs 队友的话）
     │
     ▼
  AgentRuntime  （统一运行时上下文：12+ 参数收敛为一个对象）
     │
     ▼
  Agent Loop  （ReAct + tool-use via LiteLLM）
  ├── ToolDispatcher 统一调度
  │   ├── 内置工具     ← web_search / search_history / save_artifact / memory_*
  │   ├── 沙箱工具     ← exec_code / sandbox_* （OpenSandbox 隔离执行）
  │   ├── 编排工具     ← plan_subtasks / run_subtask / wait_subtasks（DAG 依赖）
  │   ├── 任务工具     ← task_create / task_list / task_update（自动依赖链 + 委派唤醒）
  │   ├── 能力发现     ← list_capabilities / describe_capability
  │   └── MCP 工具     ← tools.toml 注册的任意 MCP server
  └── 流式卡片 → 飞书 PATCH 实时更新（含"思考中/已执行"状态填充）
     │
     ├── Memory 内循环  ← agent 用一次额外 LLM turn 决定写什么到 MEMORY.md
     │   └── 写入后自动同步到分层记忆（行级 segment 增量 embedding）
     ├── Checkpoint     ← 每轮保存执行检查点，进程重启自动恢复
     └── Skill 评估器   ← 工具调用 ≥ N？写 SKILL.md
     │
     ▼
  SQLite + FTS5  （频道隔离，WAL 模式）
     │
     ▼
  Ambient Engine  （后台）
  ├── 频道级 APScheduler cron
  ├── Heartbeat 评估器："有没有值得跟进的事？"
  ├── 沙箱生命周期巡检（空闲销毁、优雅退出清理）
  └── 有 → 主动发飞书消息；无 → SILENT
```

## 记忆架构（三层 + 向量检索）

借鉴 [memU](https://github.com/NevaMind-AI/memU) 的"文件 + 行级 segment + 增量 embedding"设计，不再把整份 MEMORY.md 塞进上下文：

```
Layer 1 — 全局记忆（Global Memory）
  长期团队知识：约定、决策、角色、技术栈
  scope = channel + agent
  每轮对话用用户消息做 query，embedding 检索 top-k 相关片段注入
  （而不是全量注入 MEMORY.md —— 记忆再多也不拖慢上下文）

Layer 2 — 任务记忆（Task Memory）
  当前大任务的进展：PRD 完成、评审结论、产物路径
  scope = channel + agent + session（一个大任务 = 一个 session，贯穿整个委派链）
  任务完成时自动记录，仅当前 session 相关时检索注入

Layer 3 — 执行记忆（Working Memory）
  最近 N 条消息（messages.db）
  按 agent 归属区分"自己说的"和"队友说的"（多 agent 身份不混淆）

检索机制：
  - 行级 segment：记忆按行切片独立 embedding（bge-m3，1024 维）
  - 增量 reconciliation：重新写入时只对新增行做 embedding，
    不变行保留向量，删除行移除 —— 几乎零成本
  - 单次 embedding 调用 + 暴力余弦排序，无 LLM 调用，毫秒级返回
  - 相关性下限过滤：无关 query 不注入任何记忆（不污染上下文）

Layer 4 — 技能库（按频道）
  复杂任务后自动生成的 SKILL.md
  语义匹配后加载到上下文
  生命周期：active → stale（30d 未用）→ archived（90d）

Layer 5 — 语义召回（Mem0，可选）
  关键决策和事实的向量索引（ChromaDB）
  namespace = chat_id（频道间完全隔离）
```

## 支持的 LLM

通过 [LiteLLM](https://github.com/BerriAI/litellm) 统一接口，设置 `LLM_MODEL` 和对应 key：

| 提供商 | `LLM_MODEL` | Key 环境变量 |
|---|---|---|
| Anthropic Claude（默认） | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| OpenAI GPT-4o | `gpt-4o` | `OPENAI_API_KEY` |
| DeepSeek | `deepseek/deepseek-chat` | `DEEPSEEK_API_KEY` |
| Google Gemini | `gemini/gemini-2.0-flash` | `GEMINI_API_KEY` |
| Groq | `groq/llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| 本地 Ollama | `ollama/llama3` | *(无需)* |
| OpenAI 兼容自建网关 | `openai/<model>` | `LLM_API_BASE` + `LLM_API_KEY` |

**频道级模型覆盖**：在 `data/channels/<id>/tools.toml` 里写 `[llm] model = "..."`，`#general` 用便宜模型，`#engineering` 用强模型。

## 内置工具

每个频道默认可用，无需配置：

**基础工具**

| 工具 | 作用 |
|---|---|
| `web_search` | DuckDuckGo 即时搜索，无需 API key |
| `search_channel_history` | 全文检索本频道历史消息 |
| `save_artifact` | 把生成物保存到 agent 工作区 |
| `thread_unfollow` / `bookmark_message` | 线程管理 / 消息书签 |
| `memory_append` / `memory_replace` / `memory_delete` | 长期记忆写入（自动同步到分层向量记忆） |

**沙箱代码执行**（[OpenSandbox](https://github.com/OpenSandbox) 隔离环境，按 session 复用）

| 工具 | 作用 |
|---|---|
| `exec_code` | 在独立沙箱中执行 Python/JS/Java/Go/Bash，变量与文件跨调用持久 |
| `sandbox_read_file` / `sandbox_write_file` / `sandbox_list_files` | 沙箱文件读写 |
| `sandbox_install_package` | 安装 pip/npm 依赖 |

**多 Agent 编排**（运行时保障执行顺序：依赖链 + 自动唤醒 + 任务追踪）

| 工具 | 作用 |
|---|---|
| `task_create` / `task_list` / `task_update` / `task_get` / `task_claim` | 任务板（同 session 自动建立依赖链，assignee 委派自动唤醒目标 agent） |
| `plan_subtasks` | 把复杂任务拆成带 DAG 依赖的子任务 |
| `run_subtask` / `wait_subtasks` / `get_subtask_status` / `retry_subtask` | 子任务执行、等待、状态查询、重试 |

**能力发现**

| 工具 | 作用 |
|---|---|
| `list_capabilities` | 按分类列出全部可用工具 |
| `describe_capability` | 查看某个工具的详细用法 |

**提醒与定时**

| 工具 | 作用 |
|---|---|
| `reminder_schedule` / `reminder_list` / `reminder_cancel` | 一次性提醒 |
| `schedule_task` / `list_crons` / `cancel_cron` | cron 定时任务 |

通过 `tools.toml` 还能接入任意 MCP server——GitHub、Linear、Notion、Jira、Datadog 等。

## 项目结构

```
ocl/
  gateway/
    feishu/
      ws_client.py    ← lark_oapi 长连接入口（含沙箱生命周期集成）
      events.py       ← 事件分发、@解析、用户名替换
      gateway.py      ← 发消息、流式卡片、文件上传
      auth.py         ← 飞书 tenant access token
    router.py         ← chat_id → AgentSession 路由（session 贯穿委派链）
  runtime/            ← 统一运行时
    context.py        ← AgentRuntime：12+ 参数收敛为一个对象
    dispatcher.py     ← ToolDispatcher：统一工具调度（exact > prefix > fallback）
    handlers.py       ← 各类工具 handler（任务/提醒/cron/记忆 + 委派自动唤醒）
    orchestrator.py   ← 子任务编排引擎（DAG 依赖、等待、重试）
    delegation.py     ← 委派：任务链创建、@mention、下游唤醒
    context_manager.py← 长上下文压缩（超阈值时 LLM 摘要）
    checkpoint.py     ← 执行检查点（进程重启自动恢复）
  agent/
    loop.py           ← ReAct 主循环、流式输出、检查点、记忆检索注入
    context.py        ← 系统提示词组装（含多 agent 消息归属）
  agents/
    config.py         ← agents.toml 解析、scopes、workspace
    task_store.py     ← 任务板（依赖链、session 过滤、活跃任务过滤）
    ledger.py         ← 执行账本（SQLite 持久化）
    cancel.py         ← asyncio 取消令牌
  memory/
    layered.py        ← 三层记忆（全局/任务/执行）+ 行级 segment 增量 embedding
    embedder.py       ← embedding 客户端（SiliconFlow bge-m3 / OpenAI 兼容）
    store.py          ← SQLite + FTS5，频道隔离
    writer.py         ← 记忆策展内循环（写后同步到分层记忆）
  tools/
    registry.py       ← 频道级工具注册，读 tools.toml
    builtins.py       ← 内置工具实现
    sandbox/          ← OpenSandbox 沙箱（provider/tools/lifecycle）
    orchestration.py  ← 编排工具 schema + handler
    capability.py     ← 能力发现工具
  ambient/
    heartbeat.py      ← 主动巡检
  llm.py              ← LiteLLM 封装、流式、网关路由
  web_admin.py        ← FastAPI 控制台后端
  web_admin_frontend.html ← React SPA（含中英 i18n）
  doctor.py           ← 健康检查
  cli.py              ← 入口：open-claude-tag-lark / open-claude-tag-lark doctor
  config.py           ← .env 配置加载
channels/
  example/            ← 复制到 data/channels/<id>/ 作为模板
  templates/          ← 全局默认模板（新群自动初始化时复制此目录）
    CHANNEL.md
    agents.toml       ← 把 feishu_app_id/secret 换成你自己的
    agents/           ← 每个 agent 的 AGENT.md 人设（含职责边界）
tests/
```

## 开发

```bash
pip install -e ".[dev]"

pytest              # 测试
ruff check .        # lint
mypy ocl/       # 类型检查
```

## 路线图

- [x] **Phase 1** — 飞书原生频道 agent
  - [x] lark_oapi WebSocket 长连接
  - [x] chat_id 路由 → 共享 AgentSession
  - [x] 多用户归属（@用户名 替换）
  - [x] ReAct agent loop（LiteLLM）
  - [x] SQLite + FTS5 频道级会话存储
  - [x] 文件化配置（CHANNEL.md / MEMORY.md / tools.toml）
  - [x] 内置工具：web 搜索、Python、频道历史
  - [x] 频道级模型覆盖
- [x] **Phase 2** — 多 Agent + 流式
  - [x] 多 bot 身份（每 agent 独立飞书 bot）
  - [x] Agent 委派（深度限制 + 上游链防环）
  - [x] 飞书互动卡片流式 PATCH 输出
  - [x] 执行账本 + 取消令牌
  - [x] Web 控制台（看板、账本、工作区、诊断）+ i18n
- [x] **Phase 3** — 记忆深化 + 运行时
  - [x] Letta 内循环记忆策展
  - [x] 三层记忆架构（全局/任务/执行）+ 行级 segment 向量检索（memU 风格）
  - [x] AgentRuntime 统一运行时 + ToolDispatcher 统一工具调度
  - [x] 长上下文压缩（超阈值 LLM 摘要）+ 执行检查点（重启自动恢复）
  - [x] Mem0 语义召回层（可选）
- [x] **Phase 4** — 沙箱 + 编排
  - [x] OpenSandbox 隔离代码执行（exec_code，按 session 复用，生命周期自动清理）
  - [x] 自主编排工具（plan_subtasks / run_subtask / wait_subtasks，DAG 依赖）
  - [x] 委派链任务追踪（session 贯穿、具体任务标题、完成自动唤醒下游）
  - [x] 能力发现（list_capabilities / describe_capability）
- [ ] **Phase 5** — 主动模式
  - [ ] APScheduler 频道级心跳
  - [ ] LLM heartbeat 评估器（SILENT / 主动发）
  - [ ] `schedule_task` 工具：agent 自建监控 cron
- [ ] **Phase 6** — 治理 + 多平台
  - [ ] 频道级审计日志（token 消耗、工具调用）
  - [ ] 硬性 token 预算（BUDGET.md）
  - [ ] Discord / Teams 适配器

完整路线图和设计决策见项目源码与 commit 历史。

## 参考

| 项目 | 作用 |
|---|---|
| [Claude Tag — Anthropic](https://www.anthropic.com/news/introducing-claude-tag) | 共享频道 AI 队友概念的原始产品 |
| [LiteLLM](https://github.com/BerriAI/litellm) | 多提供商 LLM 路由 |
| [lark-oapi](https://github.com/larksuite/oapi-sdk-python) | 飞书开放平台官方 SDK |
| [Letta (MemGPT)](https://github.com/letta-ai/letta) | 内循环记忆策展模式 |
| [memU](https://github.com/NevaMind-AI/memU) | 分层记忆的行级 segment + 增量 embedding 设计 |
| [OpenSandbox](https://github.com/OpenSandbox) | 隔离代码执行沙箱 |
| [Mem0](https://github.com/mem0ai/mem0) | 语义召回层（可选） |

## License

MIT — 自由使用、修改、自托管。

---

*本项目独立运作，与飞书 / Lark、Anthropic 无任何隶属关系。第三方平台名称仅用于互操作与说明目的，所有商标归各自所有者所有。*
