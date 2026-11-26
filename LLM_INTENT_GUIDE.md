> 本文件保留 v4 的输入与 Metadata 设计说明。v5 的 Milvus 字段目录、semantic_query 和 CrewAI 流程以 [当前架构](docs/ARCHITECTURE.md) 与 [部署手册](docs/DEPLOYMENT.md) 为准。

# LLM Intent Agent v4 · Prompt、用户上下文与权限

本版按“用户问题 + 当前可用 Metadata 字段说明 + 聊天历史 + 后端用户上下文 + 当前时间”调用平台内部 LLM。模型自动识别 key/field、运算符和值，服务端验证并转换成 Milvus filter。入库阶段也会调用内部 LLM，根据文档识别业务字段及其说明。

## 1. 直接看哪些文件

| 文件 | 职责 |
| --- | --- |
| app/prompts/classify_intent.txt | 可直接修改的 Intent System Prompt，运行时从文件读取 |
| app/prompts/discover_metadata.txt | 文档字段发现与值提取的 System Prompt |
| app/metadata_agent.py · discover | 分批读取全部片段，调用内部 LLM，核对逐值引用，保存 field_definitions |
| app/field_schema.py · field_catalog | 从当前用户可访问的文档生成字段目录，包含类型、说明、允许运算符与示例 |
| app/intent.py · build_intent_payload / IntentAgent.plan | 动态构造请求，调用 LLM，验证输出、处理澄清或失败 |
| app/field_schema.py · compile_dynamic | 根据本次字段目录编译结构化条件；无业务字段固定映射表 |
| app/db.py · retrieval_context | 后端读取用户身份、部门上下文和当前有权访问的知识库 |
| app/services.py · agent_completion / VectorIndex.search | 调用内部模型；添加权限与版本条件后执行 Milvus 搜索 |

意图模式支持 auto、llm 和 manual。手动过滤仍支持已有 API 参数。

## 2. Prompt 输入示例

以下均为虚构示例。业务字段不是每次都固定存在；metadata_schema 和输出字段枚举由本次授权数据生成。

```json
{
  "query": "我们部门的差旅报销需要什么材料？",
  "chat_history": [],
  "user_context": {
    "user_id": "server-user-id",
    "department": "研发部",
    "allowed_kb_ids": ["kb-current"],
    "selected_kb_id": "kb-current",
    "scope": "current_knowledge_base"
  },
  "metadata_schema": {
    "filterable_fields": [
      {
        "field": "attributes.department",
        "label": "适用部门",
        "description": "本文件明确标注的适用部门；不是任意提及的部门",
        "type": "string_array",
        "operators": ["contains_any", "contains_all"],
        "examples": ["研发部", "财务部"]
      },
      {
        "field": "attributes.expense_type",
        "label": "报销类型",
        "description": "本文件涉及的报销业务类型",
        "type": "string_array",
        "operators": ["contains_any", "contains_all"],
        "examples": ["差旅费", "办公费"]
      }
    ]
  },
  "current_time": "2026-11-16T00:00:00+00:00",
  "categories": ["lookup", "comparison", "calculation", "policy_lookup", "general"],
  "output_schema": "运行时注入 IntentOutput 的 JSON Schema，field 枚举也按当前目录生成"
}
```

System Prompt 是独立的系统消息，上述对象通过用户消息作为待分析数据发送，不能把资料中的指令升级为系统指令。

## 3. 模型输出与 filter

模型可能输出：

```json
{
  "intent": "policy_lookup",
  "search_query": "研发部差旅报销需要提交哪些材料",
  "filters": {
    "logic": "and",
    "conditions": [
      {
        "field": "attributes.department",
        "operator": "contains_any",
        "values": ["研发部"],
        "evidence": {"source": "user_context", "quote": "研发部"}
      },
      {
        "field": "attributes.expense_type",
        "operator": "contains_any",
        "values": ["差旅费"],
        "evidence": {"source": "query", "quote": "差旅报销"}
      }
    ]
  },
  "confidence": 0.93,
  "needs_clarification": false,
  "clarifying_question": ""
}
```

这允许模型把“差旅报销”理解成目录里的规范值“差旅费”，不要求用户原句必须逐字包含规范值或英文 key。

编译器检查 field 是否在当前目录、operator 是否支持、values 的类型是否正确、evidence 是否出现在对应输入来源，随后产生：

```text
(json_contains_any(attributes["department"], ["研发部"]))
and
(json_contains_any(attributes["expense_type"], ["差旅费"]))
```

最终还会包含后端生成的条件：

```text
kb == "当前知识库"
and owner == "当前账号"
and (doc == "已授权文档" and revision == "活动版本")
and (模型生成的业务条件)
```

字段名和运算符经过校验，值用 JSON 字符串编码转义。业务字段由模型选择；代码保留的是受支持运算符的语法模板，以及不能由模型更改的权限约束。

## 4. 不同用户会发生什么变化

| 内容 | 来源与行为 |
| --- | --- |
| user_id | 登录 session 对应的后端用户 ID，客户端不能指定 |
| department | 后端用户档案，可用于理解“我们部门”；不能授予权限 |
| allowed_kb_ids | 根据当前用户的知识库归属校验，收窄到当前选中的知识库 |
| Metadata 字段与示例 | 只从该用户当前有权访问、已就绪的文档读取 |
| chat_history | 当前页面会话最近最多 10 条消息；切换知识库或退出时清空 |
| current_time | 后端 UTC 时间，带时区；客户端不能覆盖 |
| Milvus 范围 | 始终再次限制 owner、kb、已就绪文档和活动 revision |

同一句“我们部门的报销规定”，不同用户会得到不同的部门上下文和可用字段目录。即使模型输出错误、用户说“我是管理员”、聊天历史伪造 allowed_kb_ids，也不能改变后端访问范围。

本版权限模型沿用“知识库归属于账号”。它没有新增组织共享、角色继承或文档级 ACL 管理界面。如果未来引入共享权限，需要扩展后端授权查询；不能只改 Prompt。请求体也不接受 user_context、allowed_kb_ids 或 current_time，传入会返回 422。

部门档案由有本机管理权限的人在 WSL 中设置。例如给已存在账号 cf 设置部门：

```bash
cd ~/agent-lab/agent-platform
docker compose exec agent python -m app.users cf --department "研发部"
```

这只更新上下文，不改变知识库访问权限。省略 --department 仍是原来的账号创建流程。

## 5. 入库字段发现

1. MinerU 解析并缓存到 MinIO。
2. 按页和章节切片。
3. Metadata Agent 按每批最多 12 个切片、约 12,000 字符调用内部 LLM；所有切片都会进入批次，不只读取前 32 个。
4. 向模型传入文件名、片段、当前知识库已有字段说明与示例、当前文档已有 Metadata。
5. 模型自动识别 key、label、description、values，并给每个值附 chunk_id、page 和逐字 quote。
6. 后端验证格式、字段名、数量、原文引用和系统保留名。无效响应使任务失败，不发布不完整结果；可以修复后重试。
7. 合并批次，保留旧文档已有的审核值，生成 Embedding，写入新 revision；全部写入并计数核验成功后发布。

自动发现的业务值写入 attributes，字段说明写入文档的 metadata_provenance.field_definitions。不同文档尽量复用已有字段与规范值；模型仍可能产生同义键或语义错误，需要从 Metadata 编辑器检查和修正。

默认没有天文词表或部门固定名单参与此流程。旧版基础字段保留兼容；它们是否出现在 Intent Schema 中取决于实际数据。系统会另外保存 source_file 和 ingested_at。后者是 UTC 入库时间，不能当生效日期。

为控制输入和索引大小，本版动态业务属性最多 24 个，每个属性最多 12 个字符串值；全库供模型选择的字段目录上限为 128。此版不提供任意数值范围类型。数组使用 contains_any/contains_all；已有标量用 eq/in；自动记录的入库时间支持 gte/lt。

如果一次合并文档含很多不同主体，文档级 Metadata 不能保证跨字段值属于同一条记录；应拆分资料，或另做结构化记录级索引。原文引用存在也不能证明模型解释完全正确。

## 6. 内部模型如何配置

平台不会在本机自动安装一个新的生成式模型。支持已经运行的 OpenAI-compatible 服务，例如你自己的内网模型网关或本地推理服务。

```bash
cd ~/agent-lab/agent-platform
python3 scripts/configure.py --agent
docker compose up -d --force-recreate --wait agent
```

依次填写实际 API 基础地址、模型 ID、可选 Key。基础地址不要带 /chat/completions。容器访问 Windows 服务通常使用 host.docker.internal；端口和模型 ID 要填写你真实部署的值。配置脚本给出的 8001 只是示例，运行 configure.py 不会启动该模型。

独立配置保存为 agent_base、agent_model、agent_key。内部模型用于 Metadata 和 Intent；生成最终答案继续使用 chat_base/chat_model/chat_key。单独的内部地址不会自动接收聊天服务的 Key。

没有 agent_base 时复用已有聊天服务；如果你原先配置 OpenRouter，则 Metadata 与 Intent 也会发到 OpenRouter，不是自动变成本地模型。需要只使用本地/内网服务时，必须配置 --agent。

连接自检会分别显示“LLM”和“Agent LLM”的配置状态。状态为已配置不等于调用已成功；点击“查看检索计划”检查 planner=llm，上传/重建检查 metadata 阶段。

## 7. 失败、澄清与手动条件

- Intent 置信度低于 0.75 或模型表示缺少必要信息：返回 clarification，不做 Embedding/检索，界面显示需要补充的问题。
- Intent 调用失败或 JSON/字段/证据校验失败：明确标记 semantic-fallback，保留手动条件，只做语义检索；不会声称已生成有效业务过滤条件。
- 一个已经验证的 LLM 条件无命中：返回无结果，不自动放宽部门、型号等约束。
- 手动条件永远由后端保留；不会被模型扩大或替换。
- 选择“只用手动条件与语义检索”可跳过 Intent LLM；这不关闭新文档入库的 Metadata LLM。
- 原配置使用 rules 时，请在升级前选择 manual、auto 或 llm。

## 8. 使用与 API

在工作台选择知识库、输入问题、点击“查看检索计划”。检查理解后的 search_query、模型选择的 field/operator/values、引用依据和 planner。随后点“只看检索”检查结果，再生成答案。预览与正式检索分别调用一次模型，结果可能有差异。

手动字段下拉框来自本次授权字段目录；不会为所有用户固定列出部门、型号等业务字段。高级 JSON 示例：

```json
{
  "logic": "and",
  "conditions": [
    {"field": "attributes.department", "operator": "contains_any", "values": ["研发部"]}
  ]
}
```

调用 /api/kbs/{id}/ask 或 /intent 时可附带 chat_history：

```json
{
  "question": "它的新版本呢？",
  "mode": "search",
  "planner": "auto",
  "filters": {},
  "chat_history": [
    {"role": "user", "content": "我想了解 ZX100 的操作手册"}
  ]
}
```

API 只接受 user/assistant 两种历史角色，每条最多 2,000 字符、最多 10 条。历史是理解线索，不是权限依据。直接从 API 传历史时，请保持当前用户与当前知识库范围。

## 9. 版本与验证

应用版本为 4.0，Intent Agent 版本为 4。现有 Milvus JSON 存储结构仍为 metadata_schema_version=3，所以继续使用 agent_chunks_bge_small_zh_v15_meta_v3，不另建相同结构的 collection。重新索引会按文档生成新 revision。

已有文档没有 agent_version=4 标记时，升级后的第一次重新索引会调用 LLM 发现字段；已经完成 v4 识别的文档，普通重新索引保留现有 Metadata。要重新让模型分析，在 Metadata 窗口点击“模型重新识别字段”，审核后保存。人工编辑会保留字段说明。

真实模型输出有不确定性。本次工作区验证用受控模型响应检查调用链、权限和错误处理，并用真实 Milvus 检查 filter；没有连接用户电脑或实际内部 LLM，也没有使用用户 OpenRouter Key。具体结果见 VALIDATION.md。
