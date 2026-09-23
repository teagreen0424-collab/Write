# 灵墨（Lingmo）— AI 网文协作写作工作台

> 个人自用、本地优先的中文网络小说 AI 协作写作工作台。核心是「人机共创」：作者主导构思与方向，AI 负责执行、记忆与一致性维护。

## 一句话定位

在「自动驾驶批量写」与「聊天式共创」之间取中：让 AI 帮你把世界观、人物、伏笔这些设定**结构化地沉淀**下来，同时允许你**随时把构思插进去**，而不是让 AI 一跑到底、你插不上手。

## 为什么做这个

现有开源项目大致分两派，各有短板：

- **纯自动驾驶派**（PlotPilot、harnessNovel、AI_NovelGenerator）：长篇一致性、设定管理强，但作者干预难、易跑偏、AI 味重。
- **纯对话共创派**（SillyTavern、ReNovel-AI、zenstory）：人主导、交互自然，但长文一致性弱、设定容易散。

本项目的差异化：**自动驾驶 + 构思注入**。既保留自动驾驶的生产力，又把「作者的一句话构思 → 引擎的结构化约束」这条转换链路做成核心能力。

## 核心概念

| 概念 | 含义 |
|------|------|
| Story Bible（设定圣经） | 世界观、人物、地点、时间线、伏笔等全部设定的统称，是系统最底层的状态 |
| 构思注入 | 作者把想法/构思输入系统的动作，分三种粒度（见 PRD） |
| 章前指令 | 每章生成前，作者写的一段「这章要什么走向/情绪/桥段」的强约束 |
| 自动驾驶 | 引擎在作者定好的大纲与设定约束下，自主推进多章生成 |
| 一致性检查 | 生成内容与 Story Bible 的冲突检测，防止人设崩坏、伏笔丢失 |

## 三种写作模式

1. **人工主导**：一章一章写，你写/改，并在需要时点击“定稿沉淀”，系统抽取设定建议并提示一致性冲突。
2. **自动驾驶**：你定好大纲和设定，引擎自主推进多章，你随时暂停、注入指令。
3. **对话共创**：在聊天框里用自然语言描述构思，系统自动解析成结构化约束并更新设定库。

三种模式共用同一套 Story Bible 状态，随时切换不丢设定。

## 文档索引

| 文档 | 内容 | 读者 |
|------|------|------|
| [AGENTS.md](AGENTS.md) | 开发上下文、角色分工与 git 硬约束、常用命令、当前状态与已知偏差 | Codex/Cursor/Trae 及所有开发 Agent |
| [docs/01-PRD.md](docs/01-PRD.md) | 产品需求：功能模块、交互、业务规则（基准 Spec） | 产品/开发 |
| [docs/02-技术架构.md](docs/02-技术架构.md) | 系统架构、模块划分、技术选型、关键流程（基准 Spec） | 开发 |
| [docs/03-数据模型.md](docs/03-数据模型.md) | Story Bible 数据库 schema 与核心数据结构（基准 Spec） | 开发 |
| [docs/04-开发路线图.md](docs/04-开发路线图.md) | 分阶段任务清单与验收标准（**进度台账，判断做到哪了只看这份**） | 开发/项目管理 |
| [docs/05-阶段六-全局对话系统.md](docs/05-阶段六-全局对话系统.md) | 阶段六设计与「已知偏差」（已收口） | 开发/项目管理 |
| [docs/06-阶段七-质量治理与一致性深化.md](docs/06-阶段七-质量治理与一致性深化.md) | 阶段七设计与完整验收记录（已验收通过） | 开发/项目管理 |
| [docs/07-阶段八-章节版本与成品交付.md](docs/07-阶段八-章节版本与成品交付.md) | 阶段八设计与自检：两轮已实现，待整体验收确认 | 开发/项目管理 |

## 当前状态

阶段零～六已实现并验收：

| 里程碑 | 内容 |
|------|------|
| 阶段零 | 前后端脚手架 + LLM 连通 |
| 阶段一 | Story Bible 设定库（六类实体 CRUD + 关系图谱） |
| 阶段二 | 人工主导闭环（单章写作 + 设定沉淀 + 一致性检查 + 导出） |
| 阶段三 | 自动驾驶（大纲驱动多章推进 + 暂停/注入 + 张力/漂移） |
| 阶段四 | 对话式构思注入（自然语言 → 结构化约束） |
| 记忆与检索层 | 向量检索（fastembed + NumPy）+ 语义召回 + memory_chunk/injection |
| 阶段六 | 全局对话系统（无作品也能聊、会话按作品归档、结论显式落地） |

**阶段七（质量治理与一致性深化）已验收通过**——去 AI 味扫描与改写预览、文风锚定与漂移曲线、伏笔追踪聚合、一致性双层化均已落地；改写预览“放弃”和“应用”的人工 UI 点击复验也已通过。详见 [阶段七设计与完整验收记录](docs/06-阶段七-质量治理与一致性深化.md)。

**阶段八（章节版本与成品交付）两轮均已实现，待项目管理者确认整体验收**——覆盖前快照、安全恢复、版本历史、全书统计、整本/按卷/选章导出 Markdown/TXT/DOCX、零模型调用的导出前检查及成品交付面板均已落地；复核后已有 109 项后端测试通过，前端构建/类型检查和浏览器自检通过。详见 [阶段八设计与自检](docs/07-阶段八-章节版本与成品交付.md)。

详见 [开发路线图](docs/04-开发路线图.md)。

## 快速开始

### 环境要求

- Python 3.11+、Node.js 18+

### 1. 后端

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
# 复制 .env.example 为 .env，填入 OPENAI_API_KEY（默认 DeepSeek）
copy .env.example .env
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

### 2. 前端

```bash
cd frontend
npm install
npm run dev
# 浏览器打开 http://localhost:5173/
```

前端 dev server 会把 `/api` 代理到 `http://localhost:8000`。

### 3. 嵌入模型（记忆与检索层）

- 默认本地 `bge-small-zh-v1.5`（fastembed，ONNX 轻量、无 PyTorch），首次运行会自动下载到项目内 `backend/fastembed_cache/`（已加入 .gitignore，之后离线可用，无需重复联网）。
- 若网络受限，可切云端嵌入：在 `.env` 配 `EMBEDDING_SERVICE=openai`、`EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`。
- 嵌入/检索失败会自动降级为「仅章级摘要链」，不影响主流程。

### 4. 测试

```bash
cd backend
.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
```

## 关键假设（可随时推翻）

以下决策由项目经理在缺少你明确输入时自行拍板，如与你预期不符，直接告诉我即可：

1. **产品名**「灵墨」为工作标题，可随时改名。
2. **形态**为本地 Web 应用（浏览器访问 `localhost`），暂不做桌面壳、不做移动端、不做多用户。
3. **模型**默认走 OpenAI 兼容接口，优先 DeepSeek，可切换 Doubao/Qwen/Claude/Gemini；嵌入默认本地 `bge-small-zh-v1.5`。
4. **前端**选 Vue 3，与参考项目 PlotPilot 技术栈一致，方便直接借鉴其领域建模与组件。
5. **部署**以「单机本地跑」为第一优先级，Docker 作为可选加分项，不阻塞主线。

## 参考项目

本方案大量借鉴以下开源项目的思路（非直接 fork）：

- [PlotPilot（墨枢）](https://github.com/shenminglinyi/PlotPilot)：Story Bible / 世界观 / 伏笔 / 记忆的领域建模、十步写作管线、张力评分。
- [SillyTavern](https://github.com/SillyTavern/SillyTavern)：聊天式创作交互、角色卡/世界观卡管理。
- [ReNovel-AI](https://github.com/BiranSama/ReNovel-AI)：RAG 长时记忆、作家/主编/助手多角色协作。
- [harnessNovel](https://github.com/XTmingyue/harnessNovel)：拆书 + 仿写、去 AI 味、百万字连载。

## 许可证

本项目为**个人自用**，不对外开源、不商业化。借鉴的 PlotPilot 为 Apache 2.0 + Commons Clause，允许非商业内部使用，本方案仅借鉴其设计思路，不复制其代码。
