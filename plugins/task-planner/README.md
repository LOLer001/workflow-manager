# Task Planner

Task Planner 是一个显式调用的计划门禁插件。用户在消息中调用 `$task-planner` 后，Codex 先把当前任务整理为本会话中的 Markdown 计划文件，展示该版本并等待明确确认；确认前不执行实际任务。

插件只提供 skill 指令，不使用 hook、MCP、设置或安装器。它不会替代 Workflow Manager，也不会读写 Workflow Manager 的私有状态。Hard 任务仍需满足 Workflow Manager 和宿主的独立授权要求。

## 使用

在实际任务消息中显式写出 `$task-planner`。仅讨论、引用或开发 Task Planner 不会启用它。

计划修改会生成新版本并重新等待确认。用户在聊天中对当前已保存并展示版本的明确执行同意才构成 Task Planner 授权；计划文件自身的状态或手工修改不能替代聊天授权。
