# DocuThinker 升级

最新部署、配置与使用步骤见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

请保留原项目路径、settings.json、.env 与 state，先运行新版 scripts/upgrade.py --dry-run。应用 v5 沿用 Metadata 存储 v3，无须删除旧集合。

升级前请在正在运行的旧版本中，将所有使用 v1/v2 索引的就绪文档重新索引，等待作业完成，并核对已审核的 Metadata。若设置使用 rules 意图模式，请先选择 manual（只使用手动条件）、auto 或 llm。新版升级脚本会在演练和停机后检查这两个条件。历史 v1/v2 修订保留在存储中，只有 v3 修订可在新版界面回滚。

整版恢复时停止新版服务，保留其 state 副本，再从升级脚本打印的备份路径恢复旧版代码、配置和匹配的 state，重新构建启动旧版。外部 Milvus 和 MinIO 应继续使用同一安装的原集合与桶。
