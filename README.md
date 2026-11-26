# DocuThinker

文档分析与 RAG 平台。基于现有 FastAPI 项目持续迭代，复用 MinerU、MinIO 和 Milvus，实现自动 Metadata、LLM 意图过滤，以及可选的 LangGraph + CrewAI 多 Agent 问答。

## 已实现

- PDF 上传 → MinerU pipeline 解析 → LangChain 切片 → LLM 发现业务字段 → BGE Embedding → Milvus 入库。
- 原文、Markdown、图片和 JSON 保存到 MinIO；SQLite 管理账号、知识库、任务和活动索引版本。
- 根据实际 Milvus JSON 自动生成字段目录，按用户、知识库和版本缓存，发布/回滚后自动更新。
- Intent 输入包含问题、对话历史、授权字段说明、用户上下文与当前时间；输出 semantic_query 和结构化过滤条件。
- 校验后生成 Milvus filter；登录身份、知识库和活动 revision 由后端强制限制。
- LangGraph 编排检索、重排、澄清与回答；可切换 CrewAI 的证据分析、独立核验流程。
- 原文引用、执行轨迹、失败重试、索引重建、活动版本切换和回滚。

## 快速开始

完整逐步操作见 **[部署与使用手册](docs/DEPLOYMENT.md)**。已有项目在新版本目录运行：

```bash
python3 scripts/upgrade.py --target ~/agent-lab/agent-platform --dry-run
python3 scripts/upgrade.py --target ~/agent-lab/agent-platform
```

随后在原安装目录配置实际内部模型：

```bash
cd ~/agent-lab/agent-platform
python3 scripts/configure.py --agent --intent-mode auto
docker compose up -d --force-recreate --wait agent
```

打开 http://localhost:8020 。旧配置、账号和 state 保留。`--agent` 连接已经运行的 OpenAI-compatible 服务，不安装或启动生成式模型；未配置独立端点时复用原聊天服务。

## 开发与测试

Python 3.12，建议在 Linux/WSL 运行 Milvus Lite：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
bash scripts/test_offline.sh
.venv/bin/ruff check app scripts tests
node --check app/static/app.js
```

测试使用真实 Milvus TCP 引擎、LangGraph 和 CrewAI 编排，外部模型/OCR/对象存储使用受控替身，不需要真实 API Key。[验证记录](VALIDATION.md) 区分本地测试与目标机器验收。GitHub Actions 配置在 `.github/workflows/ci.yml`。

## 代码与说明

| 内容 | 入口 |
|---|---|
| 部署与升级步骤 | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| Intent、权限、多 Agent 验收 | [docs/TESTING.md](docs/TESTING.md) |
| 当前架构与限制 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Intent Prompt | [app/prompts/classify_intent.txt](app/prompts/classify_intent.txt) |
| Intent 输入/输出与编译入口 | [app/classify_intent.py](app/classify_intent.py) |
| Metadata 自动目录 | [app/metadata_catalog.py](app/metadata_catalog.py) |
| Metadata 字段发现 | [app/metadata_agent.py](app/metadata_agent.py) |
| LangGraph 编排 | [app/graphs.py](app/graphs.py) |
| CrewAI 分析/核验 | [app/crew_agents.py](app/crew_agents.py) |

当前支持 PDF 上传和按账号隔离的知识库；Word 原生上传、共享知识库及组织 RBAC 尚未实现。保留旧规则模式仅供兼容；默认 auto/llm 路径使用动态 Schema，不依赖固定业务字段映射。

应用 v5.0；存储 Schema 沿用 v3，避免不必要的历史数据迁移。不提交 settings.json、.env、账号数据库、文档内容、模型缓存或 API Key。
