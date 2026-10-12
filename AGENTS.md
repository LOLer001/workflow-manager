# Workflow Manager Repository Guidance

- 插件源码分别位于 `plugins/workflow-manager` 和 `plugins/task-planner`，共享市场入口 `.agents/plugins/marketplace.json`。
- Workflow Manager 的内部名称保持 `workflow-manager`，显示名称保持 `Workflow Manager`；Task Planner 使用 `task-planner` 和 `Task Planner`。
- Task Planner 仅显式调用，使用 skill 指令与会话计划文档；不得添加 hook、修改 Workflow Manager 的状态或绕过其授权。
- 行为变更必须有对应测试；不要为了增加并行度而放宽共享资源保护。
- 发布时同步更新对应插件的 `.codex-plugin/plugin.json` 版本和 `CHANGELOG.md`。Workflow Manager 使用 `vX.Y.Z` 标签，Task Planner 使用独立的 `task-planner-vX.Y.Z` 标签。
- 修改后运行 `python scripts/validate_repository.py`、完整单元测试和 Windows 原生测试。
- 不提交生成缓存、原始会话内容、完整日志、密钥或个人数据。
