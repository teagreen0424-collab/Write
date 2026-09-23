# AGENTS.md — 灵墨（Lingmo）开发上下文

本文件供 Codex / Cursor / Trae 及所有开发 Agent 自动读取。开始任何编码前，先读完本文件与 [README.md](README.md)。

## 角色分工（最高优先级，先读这条）

- **项目管理者（人类）**：own 全部文档、规划、阶段划分、验收判定与最终授权。
- **Codex 桌面端主控会话（当前与项目管理者直接协作的主 Agent）**：在完成代码审阅、测试和工作区核对后，可以执行 `git add`、`git commit`、`git push` 等正常提交操作，并负责报告提交哈希与远端状态。未经项目管理者明确授权，不得执行 `reset`、`checkout`、`stash`、强制推送、改写历史等可能丢失或覆盖工作的操作。
- **其他开发 Agent**：包括 Codex CLI、子 Agent、Cursor、Trae 以及其他临时开发会话，只改授权范围内的源代码或文档，**不得执行任何 Git 写操作**——不 `git add`、不 `git commit`、不 `git push`、不建分支、不 `git reset`/`checkout`/`stash`。完成后把改动留在工作区，交由 Codex 桌面端主控会话审阅并提交。
- 其他开发 Agent 可以读 `git status` / `git log` / `git diff` 以了解上下文，但**只读不写**。
- 文档如需改动，先向项目管理者说明理由；**不得用改文档的方式掩盖实现偏差**，也不得擅自判定「改代码还是改文档」。

## 项目是什么

个人自用的中文网络小说 AI 协作写作工作台。作者主导构思，AI 负责写作执行、设定记忆与一致性维护。单用户、本地部署、非商业化。

## 当前状态与已知偏差（接手的 Agent 必读）

**进度**：阶段零～七均已实现并验收（阶段零～六各有里程碑 commit）。**阶段七已验收通过**：改写预览的“放弃”和“应用”均已人工复验通过。**阶段八“章节版本与成品交付”第 1～8 步均已实现**：第一轮第 1～4 步通过专项验收；第二轮第 5～8 步已完成自动化与浏览器自检，待项目管理者确认整体验收。设计与自检记录见 [docs/07-阶段八-章节版本与成品交付.md](docs/07-阶段八-章节版本与成品交付.md)。

**已裁决与已解决记录**：

1. **定稿模型调用采用方案 B**：未设置 `style_anchor` 时，设定抽取调用模型 1 次；设置后额外调用 `check_drift` 1 次，总计 2 次。`deep_check` 不进入定稿主流程，漂移分同步计算并立即写库。
2. **聊天 `injection` 留痕已解决**：前端应用 assistant 消息时发送实际消息正文作为 `raw_text`，并明确发送 `channel=chat`；一次确认应用写一条留痕。
3. **`outline_node_id` 透传已解决**：PUT 更新章节原本即可正确落库；`POST /chapters/write` 的缺失透传也已补齐。
4. **显式“定稿沉淀”是产品决定**：仅作者点击按钮时触发完整沉淀；普通章节更新或只把 `status` 改为 `finalized` 均不自动触发。

阶段七不存在未完成验收项或待裁决的代码偏差。完整验收历程与最终结果见 [docs/06-阶段七-质量治理与一致性深化.md](docs/06-阶段七-质量治理与一致性深化.md)。

## 常用命令

后端（**必须用项目虚拟环境的解释器**，系统 Python 缺依赖会 `ModuleNotFoundError`）：

```
# 装依赖（首次，工作目录 = backend/）
backend\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 起后端（工作目录 = backend/）
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000

# 跑全部测试 —— 工作目录必须是 backend/，`-s tests` 是相对路径，
# 在仓库根执行会报 ImportError: Start directory is not importable: 'tests'
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

前端（在 `frontend/` 下）：

```
npm install
npm run dev       # Vite 开发服务器
npm run build     # 类型检查 + 构建
npm run preview   # 预览构建产物
```

**前端没有测试脚本**，`npm test` 不存在，别去调它。阶段八第二轮自检后，后端测试共 108 项通过（原 68 项不得减少）；前端另用 `npm.cmd run build` 与 `npx.cmd vue-tsc --noEmit` 检查。

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
- **阶段三（自动驾驶，已完成）**：不新增重依赖，沿用阶段二依赖；大纲驱动多章推进，上下文仍用「章级摘要链」，**不引入向量检索与嵌入**，不建 `memory_chunk`/`injection` 表。
- **阶段四（对话式构思注入，已完成）**：不新增重依赖，沿用阶段三依赖；**不引入向量检索与嵌入**，不新增数据库表。
- **阶段五（记忆与检索层，已完成）**：新增 `fastembed`（嵌入，ONNX 轻量、无 PyTorch）+ `numpy`（向量检索）；构建向量检索层与 `memory_chunk`/`injection` 落库；数据量超过约 5 万条再考虑 `faiss-cpu` 或 `sqlite-vec`。
- **阶段六（全局对话系统，已完成）**：不新增任何依赖，沿用阶段五依赖；新增 `conversation`/`message` 两张表（`novel_id` 可空表示全局会话）；对话 API 走 SSE 流式（复用现有 streamSSE 模式）；「应用到作品」复用 `inject_idea`/`apply_injections`，禁止重复造轮子。
- **阶段七（质量治理与一致性深化，已验收通过）**：不新增任何依赖，沿用阶段五、六依赖；不新增数据库表，`chapter` 只加一列 `drift_score`（`FLOAT NULL`），靠 `db.py` 里幂等的 `_ensure_columns(engine)` 用 `PRAGMA table_info` 探测后 `ALTER TABLE` 补列（`create_all` 不会给既有表加列）。去 AI 味词表以代码常量形式落在 `application/cliche.py`（共 57 条），不建表、不做管理界面。设计见 [docs/06-阶段七-质量治理与一致性深化.md](docs/06-阶段七-质量治理与一致性深化.md)。
- **阶段八（章节版本与成品交付，已实现、待整体验收确认）**：新增 `chapter_revision` 表与轻量依赖 `python-docx`（本阶段唯一新增依赖）；已实现覆盖前快照、安全恢复、版本界面、目录统计、范围导出、导出前规则检查和成品交付面板。版本只覆盖章节作者字段，恢复后派生数据失效；不引入前端 diff 库，不做软删除。设计见 [docs/07-阶段八-章节版本与成品交付.md](docs/07-阶段八-章节版本与成品交付.md)。
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

见 [docs/04-开发路线图.md](docs/04-开发路线图.md)。阶段零～七均已完成并验收；阶段八第 1～8 步已实现，当前等待项目管理者确认整体验收。不得开始方向 B。

### 文档地图（先看「文档状态」，再决定信哪一句）

| 文档 | 状态标记 | 该怎么用 |
|------|----------|----------|
| [docs/01-PRD.md](docs/01-PRD.md) | 基准（Spec） | 产品应有的样子，不记载进度 |
| [docs/02-技术架构.md](docs/02-技术架构.md) | 基准（Spec） | 系统应有的结构与选型，目录结构已与代码对齐 |
| [docs/03-数据模型.md](docs/03-数据模型.md) | 基准（Spec） | 表结构应有样子；实际以 `infrastructure/models.py` 为准 |
| [docs/04-开发路线图.md](docs/04-开发路线图.md) | 进度台账 | **判断「做到哪了」只看这一份** |
| [docs/05-阶段六-全局对话系统.md](docs/05-阶段六-全局对话系统.md) | 已收口 | 阶段六设计，含第 10 章「已知偏差」 |
| [docs/06-阶段七-质量治理与一致性深化.md](docs/06-阶段七-质量治理与一致性深化.md) | 已验收通过 | 阶段七设计与完整验收记录 |
| [docs/07-阶段八-章节版本与成品交付.md](docs/07-阶段八-章节版本与成品交付.md) | 两轮已实现，待整体验收确认 | 阶段八施工依据与第二轮自检记录 |

四份基准文档与代码冲突时，**不要默认文档对、也不要默认代码对**，先查上表的进度台账与本文件的「当前状态与已知偏差」。

架构与数据模型见 [docs/02-技术架构.md](docs/02-技术架构.md) 与 [docs/03-数据模型.md](docs/03-数据模型.md)。

## 禁止提交的内容

- `.env`、任何 API Key、模型网关私有地址。
- `data/`、`logs/`、`*.db`、向量库文件、运行时缓存。
- 大体积模型文件（如 `bge-small-zh-v1.5` 权重，走 `gitignore` + 下载脚本）。
