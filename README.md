# Workflow Manager

面向 Codex 的质量优先、上下文高效工作流管理插件：只减少冗余和噪声，不削弱解决问题所需的推理、证据、纠错与验收验证。

## 核心能力

- 正确性与必要推理、证据、纠错和验收验证始终高于 token 或上下文节省。
- 先判断日常问题或工作问题：聊天、天气、日报、电脑清理等日常请求保持当前会话设置；设备定制、设备 Bug、App/代码开发、构建部署及工程诊断进入工作评估。
- 工作评估请求使用可用的最高模型和最高推理强度判断简单或困难；Hook 只记录和提示该策略，不能虚报宿主已经完成切模。
- 简单工作问题由评估档直接解决和验证，不额外增加计划确认轮次。
- 困难工作问题先只读取证据，再给出包含模块、文件、方法、改动、构建部署、验收、风险和回退的详细计划；只有用户严格确认当前计划后才开始写入、构建或部署。
- 严格确认后，父会话继续以高推理负责协调和复核；从宿主当时实际暴露的选项中选择最新的较低档 Codex 模型，创建唯一合同执行子智能体并固定 `reasoning_effort=medium`，不硬编码具体模型名。
- Hook 不能切换父会话模型；只有宿主接受带显式 `model` 覆盖且 `fork_turns=none` 或正整数的创建请求，才算子智能体切换证据。
- `execution_contract_id` 同时绑定目标、难度决策、计划代次和计划摘要；失败按类型记录，初次失败后最多允许一次有实质修正的恢复，禁止原样重试。
- 新增约束、范围或目标会使待确认计划失效并要求重新规划；日常/工作与简单/困难分类都不改变删除、覆盖、安装、外发等安全边界。
- 按任务复杂度选择直接处理、聚焦处理或复杂工作流。
- 复杂任务主动评估关键路径，只要预期节省时间高于协调成本，就优先并行调度独立的读、写、测试、研究或复核工作。
- Complex 最多 2 个、Extensive 最多 3 个子智能体；上限只是容量，不是固定数量，也不要求必须派一个只读子智能体。
- 日常请求、简单工作及原有并行收益判断保持不变；唯一执行合同只约束已确认困难计划的变更范围，其他并行通道不得成为第二个执行者。
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
py -3 "$CodexHome\plugins\cache\workflow-manager\workflow-manager\1.0.23\scripts\install_stable_skill.py" --codex-home "$CodexHome"
```

Linux、WSL 或 macOS：

```bash
codex_home="${CODEX_HOME:-$HOME/.codex}"
python3 "$codex_home/plugins/cache/workflow-manager/workflow-manager/1.0.23/scripts/install_stable_skill.py" --codex-home "$codex_home"
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
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.23 --json
```

如需回退，先移除插件和市场，再使用目标标签重新添加：

```bash
codex plugin remove workflow-manager@workflow-manager --json
codex plugin marketplace remove workflow-manager --json
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.13 --json
codex plugin add workflow-manager@workflow-manager --json
```

## 工作方式与边界

处理顺序是“日常/工作 → 仅工作再判简单/困难 → 独立判断 Direct/Focused/Complex/Extensive”。困难不等于必须创建子智能体，子智能体数量也不能反向决定问题难度。任务仍只按需经过 `Contract → Evidence → Change → Verify → Report`。

困难计划等待确认期间，目标读取、搜索、静态检查、计划更新、澄清问题和明确只读的子智能体调查可以继续；明确文件写入、变更型子智能体或 Git、构建打包、部署安装和设备变更会被拦截。确认只绑定当前计划、目标和难度判断；任何新增约束或重规划请求都会使原确认失效。

插件会读取 Codex 生命周期事件来判断路由、输出规模和续接状态；持久化数据只保留摘要、指纹、验收待办状态和计数，不保存原始提示词、命令或子智能体结果。大工具结果会保留给模型正常推理，插件只提示后续查询如何收窄，不会仅因为输出较大而替换必要证据。钩子属于工作流护栏，不是安全边界。子智能体可能减少主会话噪声，但不保证降低总 token 消耗。

1.0.23 增加 `work_executor_low_latest` 逻辑策略：它表示“选择宿主当前实际可用的最新较低档 Codex 模型执行已确认计划”，不是固定产品名。父会话仍以高推理负责合同、协调、恢复决策和最终验收；唯一执行子智能体使用显式模型覆盖、`reasoning_effort=medium`，并在覆盖模型时提供 `fork_turns=none` 或正整数。Hook 不能切换父模型，也不能仅凭状态字段证明子智能体切换；只有宿主接受该显式创建请求才是切换证据。若没有合格模型，必须报告类型化的 `model_unavailable`，不能静默回退或虚构模型标识。

执行合同由目标指纹、难度决策 ID、正数计划代次与已确认计划摘要共同生成。创建请求还必须带完整可执行计划、独占范围、验收与回退。初次执行失败会记录为模型、配置、创建、启动匹配、合同过期、实现、构建、部署或验证等类型；仅在修正对应原因后允许一次恢复，总尝试最多两次。第二次失败或没有实质修正时停止并交回父会话重新评估，禁止换一种命令写法原样重试。

状态 Schema 10 从 Schema 9 续接已确认计划时，只保留有效计划绑定并初始化为“尚未启动”的 `spawn_required`；不会因为旧状态写着 `confirmed` 就猜测子智能体已经创建或计划已经执行。

## 仓库结构

```text
.agents/plugins/marketplace.json       GitHub 插件市场
plugins/workflow-manager/              插件源码
  .codex-plugin/plugin.json            插件清单
  assets/stable-skill/workflow-manager/  唯一可调用 Skill 的安装源
    references/work-routing.md          困难判断、计划确认与防误拦边界
    references/confirmed-execution.md   合同执行、模型证据、失败恢复与迁移
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

提交前应同时通过仓库校验、完整 Python 测试和 Windows 12 项原生测试。GitHub Actions 会自动执行这些检查。

## 贡献与发布

行为修改请附最小复现和验证结果，避免上传原始会话、完整日志、密钥或设备隐私数据。发布时同步更新插件版本与 [CHANGELOG.md](CHANGELOG.md)，通过测试后创建同版本 `vX.Y.Z` 标签。

安装命令依据 OpenAI 官方的 [插件打包与 GitHub 市场说明](https://developers.openai.com/plugins/build/plugins)。项目采用 [MIT License](LICENSE)。
