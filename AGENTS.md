# AGENTS.md — 灵墨（Lingmo）开发上下文

本文件供 Cursor / Trae 及所有开发 Agent 自动读取。开始任何编码前，先读完本文件与 [README.md](README.md)。

## 项目是什么

个人自用的中文网络小说 AI 协作写作工作台。作者主导构思，AI 负责写作执行、设定记忆与一致性维护。单用户、本地部署、非商业化。

## 核心设计原则（不可违背）

1. **Story Bible 是唯一事实源**。世界观、人物、地点、时间线、伏笔等所有设定，都必须落库到 Story Bible，禁止散落在前端状态或硬编码里。
2. **构思注入是一等公民**。系统任何「自动生成」的路径，都必须留出作者注入构思的入口（结构化设定卡 / 章前指令 / 对话式构思）。
3. **一致性优先于流畅**。宁可生成慢一点、短一点，也不能在无人值守下产出与人设/伏笔冲突的内容。冲突必须被检测并提示，而非静默通过。
4. **本地优先**。默认零外部服务依赖（嵌入走本地轻量模型、数据库走 SQLite、向量检索走 SQLite+NumPy 轻量方案），云端 LLM 只是可替换的调用目标。

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.11+、FastAPI、SQLite |
| 前端 | Vue 3 + TypeScript + Vite + Naive UI + ECharts |
| LLM 接入 | OpenAI 兼容协议（DeepSeek 默认，可切 Doubao/Qwen/Claude/Gemini） |
| 嵌入 | 本地 `bge-small-zh-v1.5`（`fastembed`，ONNX 轻量、无 PyTorch），可切云端 OpenAI 兼容嵌入 |
| 架构 | 轻量分层：`domain` / `application` / `infrastructure` / `interfaces` |

## 依赖清单（分阶段，重要）

- **阶段一（已完成）**：只装 `fastapi`、`uvicorn[standard]`、`sqlalchemy`、`pydantic`、`pydantic-settings`。禁止引入向量库、嵌入库、PyTorch。
- **阶段二（已完成）**：不新增重依赖，沿用阶段一依赖 + `httpx`；上下文装配用「章级摘要链」，暂不引入向量检索与嵌入。
- **阶段三（自动驾驶）**：不新增重依赖，沿用阶段二依赖；大纲驱动多章推进，上下文仍用「章级摘要链」，**不引入向量检索与嵌入**，不建 `memory_chunk`/`injection` 表。
- **阶段四（对话式构思注入）**：不新增重依赖，沿用阶段三依赖；**不引入向量检索与嵌入**，不新增数据库表。
- **阶段五（记忆与检索层，当前）**：新增 `fastembed`（嵌入，ONNX 轻量、无 PyTorch）+ `numpy`（向量检索）；构建向量检索层与 `memory_chunk`/`injection` 落库；数据量超过约 5 万条再考虑 `faiss-cpu` 或 `sqlite-vec`。
- **明确不用**：`chromadb`（完整包会拖入 kubernetes/opentelemetry 等大量无用依赖）、`sentence-transformers`（会拖入数 GB 的 PyTorch）。

## 目录结构（约定）

```
lingmo-writer/
├── backend/
│   ├── app/                # 后端包（唯一代码根）
│   │   ├── domain/         # 纯领域模型（Novel/Character/.../Foreshadowing 等）
│   │   ├── application/    # 用例编排（写作、构思注入、一致性检查）
│   │   ├── infrastructure/ # 技术实现（LLM Provider、SQLite 仓储、向量检索、嵌入）
│   │   ├── interfaces/     # FastAPI 路由、依赖注入
│   │   └── main.py         # 入口
│   └── tests/              # 单元测试
├── frontend/
│   └── src/                # Vue 3 应用
└── docs/                   # 需求与设计文档
```

## 开发约定

- 后端 API 统一前缀 `/api/v1/`，返回 JSON，错误用统一 `{ "detail": "...", "code": "..." }` 结构。
- 所有持久化写操作走 SQLite，禁止并发直写；多写者场景用串行调度（参考 PlotPilot 的 Write Dispatch）。
- LLM 调用必须抽象在 `infrastructure/ai` 一个入口后，业务层不得直接 import 具体 Provider。
- 所有 Prompt 集中管理（YAML 或独立模块），禁止在业务代码里内联长 prompt。
- 中文注释、中文提交信息，提交遵循 Conventional Commits（`feat:` / `fix:` / `docs:` / `refactor:`）。
- 每个领域模型配单元测试；写作引擎的关键路径配集成测试。

## 当前任务

见 [docs/04-开发路线图.md](docs/04-开发路线图.md)。阶段一（Story Bible 设定库）、阶段二（人工主导闭环）、阶段三（自动驾驶）、阶段四（对话式构思注入）已完成，已各自打里程碑 commit。当前进入**记忆与检索层里程碑（阶段五）**：引入 `fastembed`（嵌入）+ `numpy`（向量检索），构建 `infrastructure/vector/` 向量检索层与 `memory_chunk`/`injection` 落库，让 `build_context` 语义召回历史片段。注意：模型权重（bge-small-zh-v1.5）不进 git，走下载脚本 + 缓存目录；嵌入/检索失败必须优雅降级，不阻塞主流程。具体见 [docs/02-技术架构.md](docs/02-技术架构.md) 与 [docs/03-数据模型.md](docs/03-数据模型.md)。

## 禁止提交的内容

- `.env`、任何 API Key、模型网关私有地址。
- `data/`、`logs/`、`*.db`、向量库文件、运行时缓存。
- 大体积模型文件（如 `bge-small-zh-v1.5` 权重，走 `gitignore` + 下载脚本）。
