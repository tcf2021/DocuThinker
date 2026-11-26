# DocuThinker v5 验证记录

2026-11-26。针对本次交付源码，应用版本 5.0、Intent Agent 版本 5；Metadata JSON 存储格式保持 v3。本记录不表示已更新用户 Windows / WSL 的项目。

## 已完成的自动测试

在 Python 3.12 环境中安装本版锁定依赖，完整测试 **63 passed，70 warnings，14.30 秒**，进程正常退出 0。Warnings 来自依赖弃用提示，不是测试失败。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
bash scripts/test_offline.sh --junitxml=test-results.xml
```

脚本关闭依赖遥测，并通过 pytest-socket 限制 Python socket 仅连接本机 Milvus。它不是操作系统级网络隔离。推理库按需加载，测试替身不会加载 ONNX/FastEmbed；真实 Embedding/Reranker 初始化时也显式关闭 ONNX 遥测。此前触发依赖遥测而被阻止的两次运行不计入通过结果，以上数字来自修复后的完整运行。

原有 51 项回归测试继续覆盖 Metadata 约束、旧索引迁移、接口权限、重建失败恢复、引用编号、升级备份、LLM 动态字段识别、输入依据校验和模型端点配置。

新增 12 项测试覆盖：

- 从实际 Milvus JSON 获取动态字段，SQLite 中冲突的业务值不会进入字段示例。
- 按用户、知识库和活动 revision 缓存；发布、回滚和强制刷新后更新；缓存命中仍检查权限。
- 当前 revision 的 Milvus 记录缺失时明确报错，不用旧值掩盖异常。
- 130 份文档分两批读取 Metadata，查询不传输向量和正文。
- PDF 对齐的 semantic_query 输出，以及动态过滤条件编译。
- 存在模型推断条件时，旧索引不能绕过业务过滤进入结果。
- 在真实 LangGraph 内运行真实 CrewAI：先分析、后核验，核验角色实际接收草稿和授权证据。
- 证据不足、伪造引用、核验异常时不发布未核验草稿。
- “只看检索”不启动 CrewAI，角色模型调用预算受限制。

**真实组件：** Milvus Lite 的 TCP 服务、PyMilvus、LangGraph、CrewAI。**受控替身：** Embedding、Reranker、MinerU、MinIO 和 LLM 响应。这验证调用链、结构、过滤、隔离、迁移和状态行为，不代表真实模型语义准确率或最终答案质量。没有使用用户的 API Key。

## 静态检查

以下全部通过：

```bash
.venv/bin/ruff check app scripts tests
.venv/bin/python -m compileall -q app scripts
node --check app/static/app.js
bash -n start.sh scripts/test_offline.sh
```

GitHub Actions 已提供同样的检查配置。远端 CI 的实际状态应以对应提交的 Actions 结果为准；本地通过不等于远端运行过。

## 尚需目标机器验收

1. 按 docs/DEPLOYMENT.md 升级，确认 health 返回 DocuThinker / 5.0 / Intent 5 / Metadata 3。
2. 验证 Docker 镜像构建、已有 MinIO、Windows MinerU API 8000 与 pipeline、真实模型下载和 GPU/CPU 环境。
3. 配置实际内部 LLM；逐库检查字段目录、同义表达、跨轮指代、相对时间、无匹配和澄清。
4. 使用两个账号分别导入资料，核对字段目录与检索结果隔离。当前是账号拥有知识库，尚无共享库或部门级 RBAC。
5. 分别测试单模型和 CrewAI 模式，检查原文引用、执行轨迹和核验失败提示；结合自己的文档评价效果与成本。
6. 浏览器交互与响应式布局尚未完成本版人工验收。JavaScript 语法检查不替代页面使用测试。

Metadata 逐字来源检查与引用编号校验能发现部分无来源内容，不能证明 LLM 的语义理解一定正确。
