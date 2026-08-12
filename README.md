# Workflow Manager

面向 Codex 的质量优先、上下文高效工作流管理插件：只减少冗余和噪声，不削弱解决问题所需的推理、证据、纠错与验收验证。

## 核心能力

- 正确性与必要推理、证据、纠错和验收验证始终高于 token 或上下文节省。
- 先判断日常问题或工作问题：聊天、天气、日报、电脑清理等日常请求保持当前会话模型；设备定制、设备 Bug、App/代码开发、构建部署及工程诊断进入工作评估。
- 任务类别只控制模型策略，不改变安全边界；删除、覆盖、安装、外发等高风险操作仍需正常确认。
- 按任务复杂度选择直接处理、聚焦处理或复杂工作流。
- 复杂任务主动评估关键路径，只要预期节省时间高于协调成本，就优先并行调度独立的读、写、测试、研究或复核工作。
- Complex 最多 2 个、Extensive 最多 3 个子智能体；上限只是容量，不是固定数量，也不要求必须派一个只读子智能体。
- 通过明确文件/模块所有权避免写冲突；共享构建服务器、设备或交付物只串行化实际冲突的阶段。
- 在上下文压力升高时只收窄冗余展示并提前保存检查点；必要调查继续进行。
- 压缩后复用原生摘要、计划和已验证结果，不从头重复。

## 30 秒安装

需要已登录的 Codex CLI。先添加 GitHub 插件市场，再安装插件：

```bash
codex plugin marketplace add LOLer001/workflow-manager --json
codex plugin add workflow-manager@workflow-manager --json
```

安装命令完成后，把插件内的 Skill 资产同步到不含版本号的用户级稳定路径。Windows：

```powershell
$CodexHome = Join-Path $env:USERPROFILE ".codex"
py -3 "$CodexHome\plugins\cache\workflow-manager\workflow-manager\1.0.21\scripts\install_stable_skill.py" --codex-home "$CodexHome"
```

Linux、WSL 或 macOS：

```bash
codex_home="${CODEX_HOME:-$HOME/.codex}"
python3 "$codex_home/plugins/cache/workflow-manager/workflow-manager/1.0.21/scripts/install_stable_skill.py" --codex-home "$codex_home"
```

检查安装状态：

```bash
codex plugin list --json
```

同步成功时会输出 `"status": "installed"`、`"updated"` 或 `"current"`，目标固定为 `$CODEX_HOME/skills/workflow-manager`。随后重启 Codex 并新建会话，以重新加载 Skills 目录。Hook 会在 `SessionStart` 自动补建或更新稳定副本，但显式同步可以保证安装后的第一条新任务就能发现它。若稳定目录已存在但不带 Workflow Manager 受管标记，安装器会拒绝覆盖。

Workflow Manager 不会仅因新版 Hook 已接管就删除旧版本缓存：旧任务仍可能保留原有版本化注入记录。插件不会直接改写 Codex 的 rollout JSONL、SQLite、索引或活动任务文件。若团队策略限制 GitHub 市场，请先让管理员允许该仓库来源。

## 使用

插件允许 Codex 按任务自动调用，也可以显式指定：

```text
Use $workflow-manager to complete this task.
```

如需让所有任务默认使用，可在个人 `AGENTS.md` 中加入：

```md
- Use `$workflow-manager` by default in every conversation and task.
```

Workflow Manager 以总完成时间为调度目标，不会为了“看起来并行”而机械创建子智能体。构建、部署、重启和同一设备验证通常仍由一个执行者串行完成，但不会阻塞与这些共享资源无关的源码、测试、研究或复核工作。

## 升级与回退

跟随仓库默认分支升级：

```bash
codex plugin marketplace upgrade workflow-manager --json
codex plugin add workflow-manager@workflow-manager --json
```

升级后再次运行对应平台的 `install_stable_skill.py` 命令。1.0.17 及后续版本的已注册钩子会自动跨版本续接当前任务；稳定 Skill 同步完成并重启 Codex 后，新任务会从无版本路径加载。

生产环境可固定到发布标签：

```bash
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.21 --json
```

如需回退，先移除插件和市场，再使用目标标签重新添加：

```bash
codex plugin remove workflow-manager@workflow-manager --json
codex plugin marketplace remove workflow-manager --json
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.13 --json
codex plugin add workflow-manager@workflow-manager --json
```

## 工作方式与边界

复杂任务按需经过 `Contract → Evidence → Change → Verify → Report`，不会强制简单问题走完整流程。

插件会读取 Codex 生命周期事件来判断路由、输出规模和续接状态；持久化数据只保留摘要、指纹、验收待办状态和计数，不保存原始提示词、命令或子智能体结果。大工具结果会保留给模型正常推理，插件只提示后续查询如何收窄，不会仅因为输出较大而替换必要证据。钩子属于工作流护栏，不是安全边界。子智能体可能减少主会话噪声，但不保证降低总 token 消耗。

1.0.21 中的 `current` 与 `work_assessment` 是可审计的模型策略档位：Hook 会记录并解释判断，但不会虚报已经切换当前会话模型。工作问题的简单/困难判定和受控模型执行将在后续版本逐步加入。

## 仓库结构

```text
.agents/plugins/marketplace.json       GitHub 插件市场
plugins/workflow-manager/              插件源码
  .codex-plugin/plugin.json            插件清单
  assets/stable-skill/workflow-manager/  稳定 Skill 的安装源
  hooks/hooks.json                     生命周期钩子
  scripts/install_stable_skill.py      用户级稳定路径安装器
  scripts/                             其他跨平台运行脚本
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

提交前应同时通过仓库校验、完整 Python 测试和 Windows 10 项原生测试。GitHub Actions 会自动执行这些检查。

## 贡献与发布

行为修改请附最小复现和验证结果，避免上传原始会话、完整日志、密钥或设备隐私数据。发布时同步更新插件版本与 [CHANGELOG.md](CHANGELOG.md)，通过测试后创建同版本 `vX.Y.Z` 标签。

安装命令依据 OpenAI 官方的 [插件打包与 GitHub 市场说明](https://developers.openai.com/plugins/build/plugins)。项目采用 [MIT License](LICENSE)。
