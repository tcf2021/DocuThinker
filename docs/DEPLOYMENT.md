# DocuThinker 部署与使用手册

本版在原项目基础上升级，应用 v5.0，Metadata 存储仍为 v3。以下终端命令在 WSL Ubuntu 执行，只有 MinerU 启动操作在 Windows 完成。部署完成后按 [验收与排错手册](TESTING.md) 检查自动 Intent、双账号权限隔离及真实多 Agent 执行。

## 1. 检查现有服务和项目

```bash
docker version
docker compose version
docker ps -a --filter 'label=com.docker.compose.service=minio'
```

Docker 不可用时，启动 Docker Desktop，并为当前 Ubuntu 开启 WSL Integration。确认 MinIO 容器处于运行状态。原项目在 D 盘的路径曾是 `/mnt/d/AI/Dify/homework`，不需要移动它。

确认 Agent 的原安装目录：

```bash
cd ~/agent-lab/agent-platform
ls app/main.py scripts/configure.py compose.yaml
test -s settings.json && echo '平台配置存在'
```

若目录不存在，用实际容器标签定位，不要继续在错误目录运行：

```bash
docker ps -a --filter 'label=com.docker.compose.service=agent' \
  --format '{{.Names}} | {{.Label "com.docker.compose.project.working_dir"}}'
```

后面的 `--target` 要改成标签中对应的真实 Agent 目录，不能填 MinIO/Dify 目录。

## 2. 获取源码到独立目录

```bash
mkdir -p ~/agent-lab/docuthinker-upgrade
cd ~/agent-lab/docuthinker-upgrade
git clone https://github.com/tcf2021/DocuThinker.git
cd DocuThinker
ls scripts/upgrade.py app/classify_intent.py app/crew_agents.py
```

也可解压完整源码 ZIP。解压目录必须与正在运行的原项目不同，不能互相包含。若本地已有未合并代码，先对照合并；升级脚本会备份旧目录，之后覆盖交付源码。

## 3. 升级已有平台

请先按 [升级说明](../UPGRADE.md) 在旧版本完成索引与意图模式检查。

在新源码目录执行：

```bash
python3 scripts/upgrade.py --target ~/agent-lab/agent-platform --dry-run
```

核对显示的旧项目和备份位置，再执行：

```bash
python3 scripts/upgrade.py --target ~/agent-lab/agent-platform
```

脚本停止 Agent，备份源码、配置、SQLite、向量和本地数据，再构建镜像并启动。可重新下载的模型缓存不复制到备份；原缓存保留。不会删除 MinIO 对象或旧 Milvus 集合。

```bash
cd ~/agent-lab/agent-platform
docker compose ps agent
curl -fsS http://localhost:8020/health
```

应出现 `project: DocuThinker`、`version: 5.0`、`intent_agent_version: 5`、`metadata_schema_version: 3`。构建/启动失败时查看：

```bash
docker compose logs --tail 100 agent
```

## 4. 首次部署分支

只有尚未安装过平台时才用本节，不与第 3 节混用。在准备长期保留的 DocuThinker 目录：

```bash
python3 scripts/configure.py --chat
mkdir -p state
docker compose build agent
docker compose run --rm agent python -m app.users cf
docker compose up -d --wait agent
```

按提示填写现有 MinIO 地址、桶、用户名/密钥及 Docker 网络。脚本可以参考 `/mnt/d/AI/Dify/homework/.env`。账号密码由你创建，本项目没有通用默认密码。配置文件不会提交到 GitHub。

## 5. 配置内部 LLM 和最终问答模型

在实际安装目录执行：

```bash
python3 scripts/configure.py --agent --intent-mode auto
```

依次填写实际 `agent_base`、`agent_model` 和可选 `agent_key`。

- API 基础地址不包含 `/chat/completions`，例如你真实服务的 `http://host.docker.internal:端口/v1`。
- model 填服务实际公布的模型 ID，不是显示昵称。
- 端口、模型和 Key 都由你的服务决定；脚本不会安装、下载或启动生成式模型。
- 单独 agent_base 不继承 chat_key，避免将其他服务的密钥发给新端点。
- 未设置 agent_base 时复用 chat_base/chat_model/chat_key；原聊天服务为 OpenRouter 时，Metadata/Intent 也会调用 OpenRouter。
- 单模型答案使用聊天配置；CrewAI 两个角色使用 Agent 内部模型配置。

需要更新最终问答模型时：

```bash
python3 scripts/configure.py --chat
docker compose up -d --force-recreate --wait agent
```

仅修改配置文件也要重建运行中的容器。修改 Prompt 文件需重新构建镜像：

```bash
docker compose up -d --build --wait agent
```

## 6. MinerU、MinIO 与首次使用

1. Windows 上启动已有 MinerU API，例如原 `API-DIFY.cmd`，保持窗口开启，解析后端选择 `pipeline`。
2. `http://127.0.0.1:7860` 是 Gradio 页面，不等于 API；本平台需要你已配置的 API 地址，原部署使用 8000。
3. 浏览器打开 `http://localhost:8020`，使用已有账号登录；升级保留原账号。
4. 点击“检查服务”。先解决 MinerU API、MinIO 或 Milvus 连接问题。
5. 点击“预热并检查模型”。首次 BGE Embedding/Reranker 下载需要联网与磁盘空间。
6. 创建知识库并上传 PDF。当前不接受原生 Word；需要先转换为 PDF。
7. 等待 original → mineru → artifacts → split → metadata → embed_index → publish → ready。metadata 阶段会真实调用你的内部 LLM，不能只配一个不可用的示例地址。
8. 在文档详情查看 Metadata、字段说明和来源依据。模型可能理解错误，可人工修正后保存，等待新 revision 发布。

v4 已有字段无需强制重新识别；v1/v2 文档需要“重新索引”。普通重建保留已经审核的 v4 Metadata；要让模型重新分析，打开 Metadata 编辑器点击“模型重新识别字段”，审核后保存。

## 7. 验证自动字段目录与 Intent

1. 选择知识库，点击“刷新字段目录”。平台从该用户该库当前活动 Milvus revision 读取字段，不扫描向量列和正文。
2. 上传含不同字段的新资料，例如部门、适用对象、产品系列；入库完成后刷新，不必改代码里的 key 列表。
3. 输入“我们部门的差旅报销要哪些材料”，点击“查看检索计划”。
4. 检查 planner=llm、semantic_query、动态 field/operator/values，以及 Metadata 目录来源和缓存状态。
5. 打开“Milvus 筛选条件与候选数”，核对最终过滤式包含后端 owner、kb、doc/revision 范围和业务条件。
6. 条件无命中时应返回无结果，不自动删除模型条件；歧义或低置信度时应要求澄清。
7. 切换账号检查不能读取对方的库、字段示例或文档。当前权限是账号拥有知识库，尚无共享库/RBAC。

可选设置部门上下文：

```bash
docker compose exec agent python -m app.users cf --department '研发部'
```

这只修改“我们部门”等表达的解释上下文，不授予访问其他账号文档的权限。权限检查每个请求执行，包括 Metadata 缓存命中时。

缓存默认 60 秒，按用户/库/活动版本隔离；入库、重建和回滚会自动更新指纹，外部直接修改 Milvus 时可手动刷新。`settings.json` 中 `metadata_cache_ttl` 可设为 0–300 秒，重启后生效。

## 8. 使用多 Agent 模式

1. 先用“只看检索”确认片段和过滤条件正确。
2. “回答流程”选择“多 Agent：分析与独立核验”。
3. 点击“生成答案”。LangGraph 完成同一套权限、Intent、Milvus 检索和重排后，进入 CrewAI 分支。
4. Evidence Analyst 生成带证据编号的草稿，Evidence Reviewer 接收草稿和同一批授权证据进行核验。
5. 后端再次验证编号，返回答案与原文引用。展开“Agent 执行步骤”查看节点、时长和角色调用记录。
6. `insufficient` 表示证据不足；`review_failed` 表示核验异常或输出非法，平台不会发布未核验草稿。
7. 多 Agent 通常多两次模型调用；每个角色最多三次 transport 调用，每次超时 60 秒。该模式为实验选项，默认仍为单模型，不声称一定更准确或更快。

两个角色没有跨用户共享记忆，也不拥有 Shell、文件或外部检索工具。“只看检索”不会启动 CrewAI。

## 9. 接口调试

登录后的 `http://localhost:8020/docs` 可查看接口。请求使用登录 cookie 或 Bearer token；不要将 token 写进仓库或截图。

| 接口 | 用途 |
|---|---|
| GET /api/kbs/{kb_id}/metadata | 当前授权字段目录及缓存信息 |
| GET /api/kbs/{kb_id}/metadata?refresh=true | 强制从 Milvus 刷新 |
| POST /api/kbs/{kb_id}/intent | 查看模型计划 |
| POST /api/kbs/{kb_id}/ask | 检索/单模型/CrewAI 问答 |
| GET /api/agents | 当前 LangGraph Mermaid 图及角色说明 |
| POST /api/documents/{doc_id}/metadata/propose | LLM 重新拟定字段 |
| PUT /api/documents/{doc_id}/metadata | 审核保存并排队重建 |
| POST /api/kbs/{kb_id}/reindex | 逐库重建 |
| POST /api/documents/{doc_id}/index/rollback | 恢复保留的活动版本 |

问答请求示例：

```json
{"question":"我们部门的差旅报销有哪些规定？","mode":"chat","planner":"auto","answer_engine":"crewai","chat_history":[],"filters":{}}
```

用户身份、allowed_kb_ids 和 current_time 由后端提供，请求体不能伪造。历史对话也不能授予权限。

## 10. 回滚与问题排查

- 更新失败：查看升级脚本打印的备份路径。先停止 Agent，保留失败目录，再从备份恢复原目录。不要运行 `down -v` 删除数据。
- 单文档重建失败：旧活动索引继续可用，修复模型/MinIO 后重试任务。
- “Milvus 缺少当前版本 Metadata”：检查实际 milvus_uri、集合和本地向量文件，重新索引对应文档。
- planner=semantic-fallback：内部模型连接或 JSON/字段/依据校验失败；检查模型 ID、Prompt 和 Key。此状态不代表 LLM Intent 已成功。
- 核验失败：先用单模型和“只看检索”定位，再检查内部模型是否正确返回 JSON。
- CrewAI/Pydantic 安装冲突：使用本版完整 requirements.txt；它已固定兼容 Pydantic 2.12.5，不要直接叠加旧版 2.14.2 的锁文件。

目标机器仍需验证 Docker 构建、真实 MinerU/MinIO、BGE 模型下载及真实 LLM 质量。本仓库自动测试不使用你的 Key。
