# DocuThinker 简历条目与事实边界

**DocuThinker 文档分析与 RAG 平台｜Agent 工程与 AI 应用**

- 基于 FastAPI、LangChain 与 LangGraph 构建文档分析和 RAG 问答流程，复用 MinerU 解析 PDF，通过 MinIO 保存原文与解析产物，结合 BGE Embedding、Milvus 向量检索和 Reranker 返回可追溯的原文引用。
- 接入 LLM 自动识别文档 Metadata 字段、说明与取值，设计基于用户问题、对话历史和授权字段目录的 Intent Agent，将结构化条件校验并编译为 Milvus filter；实现字段目录自动读取、版本缓存及账号与知识库隔离。
- 使用 LangGraph 编排入库、意图分类、过滤检索与回答流程，接入 CrewAI 探索“证据分析—独立核验”多 Agent 模式，增加节点执行轨迹、引用编号校验、澄清与异常处理。
- 使用 Docker Compose 组织服务运行，提供索引重建、版本切换与回滚；通过 pytest 验证权限隔离、Metadata 筛选和多 Agent 流程，并配置 GitHub Actions 自动检查。

## 面试时如何说明

项目沿用现有 FastAPI/原生 JavaScript 实现，本次没有实现 React、Node.js、Express、GraphQL、Redis、RabbitMQ、MongoDB/Firebase、Google Cloud NLP、Kubernetes、Jenkins、VS Code 扩展或 React Native/Expo。

当前上传入口支持 PDF；未完成原生 Word 上传、独立摘要接口或情感分析，不写入功能列表。Milvus 默认是 Lite 本地部署，也可连接已有服务；不描述为已验证的大规模分布式集群。

测试使用真实 Milvus TCP 引擎、真实 LangGraph 和 CrewAI 编排，模型响应使用受控替身。不宣称已获得真实 LLM 的准确率、速度提升或线上用户规模。GitHub Actions 是否已在远端通过，以对应提交的实际运行结果为准。

原简历提供的日期为 2026.01–2026.05；本次 Metadata/Intent/CrewAI 继续优化记录于 2026.11。建议写“2026.01–2026.05，后续持续迭代”，或按实际参与日期调整，不把本次新增能力回填为五月前已完成。
