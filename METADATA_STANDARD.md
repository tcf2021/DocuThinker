# 通用 Metadata 存储格式 v3（Agent v4）

Agent v4 的自动字段发现、动态 Prompt、用户上下文和权限以 `LLM_INTENT_GUIDE.md` 为准。本文件主要记录兼容存储结构；其中规则提取、旧 API 例子属于历史兼容行为。

这是本项目所有知识库共用的应用数据契约，不是 Milvus 规定的行业字段标准。人事制度、产品手册、合同、论文和天文资料使用相同的顶层结构。业务差异由值和 `attributes` 表达，不需要每个知识库改数据库表结构。

## 1. 三层结构

| 层级 | 由谁生成 | 内容 |
| --- | --- | --- |
| 系统层 | 服务端 | owner、kb、doc、revision、source_sha256、schema_version、embedding_model、page、section |
| 通用文档层 | 规则、模型提案或人工 | title、doc_type、organization、version、language、effective_from、effective_to、topics、locations、entities、entity_ids、tags |
| 扩展属性层 | 明确标注提取、模型提案或人工 | attributes：统一的“属性名 → 字符串数组”结构 |

系统层不能由用户或模型覆盖。文档层 Metadata 在同一文档的切片间共享；每个切片另有页码、章节和 `chunk_mentions`。切片局部提及与文档整体提及分开保存。

## 2. 通用字段

| 字段 | 类型与空值 | 含义 |
| --- | --- | --- |
| title | string，未知 `""`，最多 240 字符 | 文档标题 |
| doc_type | 枚举，未知 `other` | policy、exception、ledger、incident、receipt、manual、report、contract、invoice、article、faq、record、other |
| organization | string，未知 `""`，最多 120 字符 | 文档所属机构；不是正文出现的所有机构 |
| version | string，未知 `""`，最多 40 字符 | 文档本身版本；不把正文引用的旧版当作自身版本 |
| language | zh / en / mixed / unknown | 当前实现支持的语言标签；规则判定是粗略识别，可人工修正 |
| effective_from / effective_to | `YYYY-MM-DD` 或 `""` | 明确的生效起止日期；未知不等于无限有效 |
| topics | string[]，未知 `[]` | 文档主题 |
| locations | string[]，未知 `[]` | 文档提及的地区、地点或站点 |
| entities | string[]，未知 `[]` | 文档提及的机构、人物、产品等名称 |
| entity_ids | string[]，未知 `[]` | 文档提及的对象编号；转大写、去重 |
| tags | string[]，未知 `[]` | 关键词或人工标签 |
| attributes | object，未知 `{}` | 扩展属性；每个值必须为非空字符串数组 |

除 entity_ids 最多 200 项外，通用数组最多 40 项；每项 1–100 字符。Metadata 总大小不得超过 48,000 UTF-8 字节，为切片追溯信息留出空间。日期格式和起止顺序会验证。未定义的顶层字段会拒绝。

`topics`、`locations`、`entities`、`entity_ids` 不等于适用范围。例如“本规定不适用于上海”，其中的上海可以是提及地点，但不能据此断言规定适用于上海。`attributes` 只填写明确标注或已经审核的属性；正文有多个部门的不同条款时，单个文档级属性不能替代条款级适用判断。

## 3. 扩展属性约定

```json
{
  "attributes": {
    "department": ["研发部"],
    "product": ["ZX-100"],
    "expense_type": ["差旅费"]
  }
}
```

- 属性名：小写英文字母开头，其后只允许小写英文字母、数字、下划线，最多 40 字符。
- 属性值：必须是字符串数组，即使只有一个值；最多 12 项，每项 1–100 字符。
- 每份文档最多 24 个属性；未知属性不填，不能用空数组或 `null`。
- 不同文档中的相同概念使用相同键和值。例如部门统一为 `department`，不要一份用 `dept`，另一份用 `department_name`。系统提供常见键的提取别名和模型提示；不会自动证明任意自定义键互为同义词。
- 系统保留名 owner、kb、doc、revision、vec、text、page、metadata、schema_version、source_sha256 不能用作属性名。
- 本版属性只支持精确匹配/集合包含。`"100"` 是字符串，不能当作数值范围查询。金额、日期、单位混合的复杂条件需额外实现相应类型与运算，不由当前属性过滤器猜测。

| 统一键 | 含义 | 示例 |
| --- | --- | --- |
| department | 部门 | `["研发部"]` |
| product | 产品或型号 | `["ZX-100"]` |
| category | 业务类别 | `["采购"]` |
| expense_type | 报销类型 | `["差旅费"]` |
| supplier | 供应商，自定义键示例 | `["示例供应商"]` |
| instrument | 仪器，自定义键示例 | `["示例光谱仪"]` |

这些是同一结构下的属性键，不是多个领域专用的数据库 Schema。自定义键仍应保持团队内命名一致。

## 4. 旧版规则提取（兼容说明）

**以下是 v3 历史规则行为，仅在显式调用旧 rules 提案接口时使用；不是 Agent v4 默认流程。** 从标题、文件名识别类型与版本；从明确标注行识别机构、日期、主题、地点、对象和属性。未知留空。没有标注的自然语言信息不会被假装已识别。

规则支持的写法：

```text
员工报销制度 V2.0
所属单位：示例公司
生效日期：2026-01-01
有效至：2026-12-31
主题：差旅报销
地点：上海
适用部门：研发部
报销类型：差旅费
属性 approval_level: 部门主管
```

通用数组支持“主题/主题词/topics”“地点/地区/站点/locations”“实体/对象/entities”“编号/实体编号/entity_ids”“标签/关键词/tags/keywords”等明确标注。数组值以逗号、顿号或分号分隔。编号也会通过带连字符代码正则提取。

属性支持常见标注“适用部门/部门”“产品型号/产品”“业务类别/资料类别”“报销类型/费用类型”，也支持任意合法键的 `属性 supplier: 示例供应商` 或 `attribute.supplier: 示例供应商`。只有独立标注行触发这些规则，不从任意包含关键词的句子断言适用关系。

**旧版可选模型提案说明：** 在文档详情点击 Metadata → “请模型拟定”。使用现有 Chat Completions 模型配置，发送文件名、同一通用 Schema、前 32 个切片。模型填写通用字段和扩展属性，并为每个字段/每个属性提供页码与逐字引用。属性证据键如 `attributes.department`。

系统核对引用是否出现在对应页、JSON 类型和大小是否有效。引用存在不代表语义正确；在编辑框检查后，点击“保存并重新索引”才发布。没有额外部署专用小语言模型，也没有默认自动调用云模型生成 Metadata。

Embedding 仍使用本地 BAAI/bge-small-zh-v1.5；重排仍使用 BAAI/bge-reranker-base。它们不生成业务属性。

## 5. 完整示例

```json
{
  "title": "员工报销制度",
  "doc_type": "policy",
  "organization": "示例公司",
  "version": "2.0",
  "language": "zh",
  "effective_from": "2026-01-01",
  "effective_to": "2026-12-31",
  "topics": ["差旅报销"],
  "locations": ["上海"],
  "entities": [],
  "entity_ids": [],
  "tags": ["已审核"],
  "attributes": {
    "department": ["研发部"],
    "expense_type": ["差旅费"]
  }
}
```

合同、论文、产品手册沿用完全相同的字段；无需填写不适用的属性。示例数据均为测试用虚构内容。

## 6. Milvus 的对应存储

新集合为 `agent_chunks_bge_small_zh_v15_meta_v3`，关闭动态 `$meta`，显式定义 Schema。

| Milvus 字段 | 内容 |
| --- | --- |
| id / owner / kb / doc / revision | 主键、权限和活动文档版本 |
| text / page | 原始切片正文、页码 |
| doc_type / version / organization | 从同一份校验后的 Metadata 生成的独立标量 |
| effective_from / effective_to | YYYYMMDD 整数，未知为 0 |
| metadata（JSON） | 完整通用 Metadata 和切片追溯信息 |
| attributes（JSON） | 从同一份 Metadata 复制的扩展属性，便于直接过滤 |
| vec | 512 维向量 |

attributes 单独作为 JSON 字段，过滤路径只有一层，如 `attributes["department"]`；不依赖对深层 JSON 对象的过滤。完整 Metadata 中保留 attributes 供查看，两个位置由同一对象同步写入。没有添加独立的 JSON/标量加速索引，向量索引仍为 FLAT/COSINE。

## 7. 旧 API 格式兼容说明

用户问：“研发部的差旅费如何报销？”如果库中已经有对应属性值，规则/模型计划可以产生：

```json
{
  "attributes": {
    "department": ["研发部"],
    "expense_type": ["差旅费"]
  }
}
```

服务端编译为：

```text
json_contains_any(attributes["department"], ["研发部"])
and json_contains_any(attributes["expense_type"], ["差旅费"])
```

还会加入账号、知识库、就绪文档和活动 revision 条件，之后在 Milvus 过滤范围内做向量搜索。模型只产生结构化值，不能提供可执行表达式。属性键和值必须来自当前库的候选目录且有问题文本依据；纯同义改写可能不会转为过滤，此时仍参与语义检索。

同字段/同属性多个值是 OR；不同字段/不同属性是 AND。手动过滤始终保留；没有匹配时不会自动换一个部门。自动推断可以在无命中时撤回，并在检索计划中提示。文档缺少某属性时，不会匹配针对该属性的显式条件。

网页“限定检索范围”支持类型、版本、编号、地点、主题、对象，以及扩展属性 JSON。完整 API 还支持 organizations、tags、effective_at。`effective_at` 仅匹配起止边界均已确认并包含该日的文档。

## 8. v1/v2 字段兼容转换

- v1：从 MinIO 缓存的 Markdown/content_list.json 提取通用字段，重新 Embedding。
- v2：保留已审核的标题、机构、版本、标签等；`sites` → `locations`，`data_types` → `topics`。south/north 转为南站/北站；calibration/raw_science/preview/audit 转为定标/原始科学数据/预览/审计。未知的自定义值原样保留，不重新用规则覆盖人工 Metadata。
- v3：普通重建保留已有 Metadata。主动点击“从原文拟定”会在编辑器重新提出规则结果；保存前请检查。
- 旧字段输入作为兼容别名接受，但输出和新入库统一为 v3 字段。旧 collection 和 revision 保留，可以回退。
- 旧 v1/v2 文档未迁移时支持兼容向量检索；显式使用通用 Metadata 过滤时会排除未迁移文档并提示。

JSON Schema 文件见 `examples/metadata-schema.json`、`examples/filter-schema.json`，运行中可调用 `GET /api/metadata/schema`。人事、产品、合同示例见 `examples/generic-metadata-examples.json`。

实现参考：[Milvus 2.5 JSON 字段](https://milvus.io/docs/v2.5.x/use-json-fields.md)、[JSON 过滤运算](https://milvus.io/docs/v2.5.x/json-operators.md)。项目实际验证范围见 `VALIDATION.md`。
