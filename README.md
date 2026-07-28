# Workflow Manager

面向 Codex 的工作流管理插件：减少无效上下文、避免重复工作，在确有独立工作线时才使用子智能体，并在会话压缩后连续推进。

## 核心能力

- 按任务复杂度选择直接处理、聚焦处理或复杂工作流。
- 只把独立、可立即开始、范围不重叠的工作交给子智能体。
- 共享代码、构建服务器、设备或交付物时保持串行。
- 在上下文压力升高时收窄输出并提前保存检查点。
- 压缩后复用原生摘要、计划和已验证结果，不从头重复。

## 30 秒安装

需要已登录的 Codex CLI。先添加 GitHub 插件市场，再安装插件：

```bash
codex plugin marketplace add LOLer001/workflow-manager --json
codex plugin add workflow-manager@workflow-manager --json
```

检查安装状态：

```bash
codex plugin list --json
```

安装后请新建 Codex 会话，插件中的技能和钩子不会热加载到旧会话。若团队策略限制 GitHub 市场，请先让管理员允许该仓库来源。

## 使用

插件允许 Codex 按任务自动调用，也可以显式指定：

```text
Use $workflow-manager to complete this task.
```

如需让所有任务默认使用，可在个人 `AGENTS.md` 中加入：

```md
- Use `$workflow-manager` by default in every conversation and task.
```

Workflow Manager 不会为了“看起来并行”而创建子智能体。构建、部署、重启和同一设备验证通常仍由一个执行者串行完成。

## 升级与回退

跟随仓库默认分支升级：

```bash
codex plugin marketplace upgrade workflow-manager --json
codex plugin add workflow-manager@workflow-manager --json
```

生产环境可固定到发布标签：

```bash
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.12 --json
```

如需回退，先移除插件和市场，再使用目标标签重新添加：

```bash
codex plugin remove workflow-manager@workflow-manager --json
codex plugin marketplace remove workflow-manager --json
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.12 --json
codex plugin add workflow-manager@workflow-manager --json
```

## 工作方式与边界

复杂任务按需经过 `Contract → Evidence → Change → Verify → Report`，不会强制简单问题走完整流程。

插件会读取 Codex 生命周期事件来判断路由、输出规模和续接状态；持久化数据只保留摘要、指纹和计数，不保存原始提示词、命令或子智能体结果。钩子属于工作流护栏，不是安全边界。子智能体可能减少主会话噪声，但不保证降低总 token 消耗。

## 仓库结构

```text
.agents/plugins/marketplace.json       GitHub 插件市场
plugins/workflow-manager/              插件源码
  .codex-plugin/plugin.json            插件清单
  skills/workflow-manager/SKILL.md     唯一技能
  hooks/hooks.json                     生命周期钩子
  scripts/                             跨平台运行脚本
  tests/                               策略与 Windows 原生测试
```

## 开发与测试

仓库一致性检查：

```bash
python scripts/validate_repository.py
```

Linux、WSL 或 macOS：

```bash
python -m unittest discover -s plugins/workflow-manager/tests -p "test_*.py" -v
```

Windows 原生测试：

```powershell
Set-Location plugins/workflow-manager
py -3 -m unittest -v tests.test_windows_hook
```

提交前应同时通过仓库校验、完整 Python 测试和 Windows 9 项原生测试。GitHub Actions 会自动执行这些检查。

## 贡献与发布

行为修改请附最小复现和验证结果，避免上传原始会话、完整日志、密钥或设备隐私数据。发布时同步更新插件版本与 [CHANGELOG.md](CHANGELOG.md)，通过测试后创建同版本 `vX.Y.Z` 标签。

安装命令依据 OpenAI 官方的 [插件打包与 GitHub 市场说明](https://developers.openai.com/plugins/build/plugins)。项目采用 [MIT License](LICENSE)。
