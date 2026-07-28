# Workflow Manager Repository Guidance

- 插件源码位于 `plugins/workflow-manager`，市场入口位于 `.agents/plugins/marketplace.json`。
- 插件、技能和目录的内部名称必须保持为 `workflow-manager`，显示名称保持为 `Workflow Manager`。
- 行为变更必须有对应测试；不要为了增加并行度而放宽共享资源保护。
- 发布时同步更新 `.codex-plugin/plugin.json` 版本、`CHANGELOG.md` 和 `vX.Y.Z` 标签。
- 修改后运行 `python scripts/validate_repository.py`、完整单元测试和 Windows 原生测试。
- 不提交生成缓存、原始会话内容、完整日志、密钥或个人数据。
