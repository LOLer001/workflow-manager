# Workflow Manager

面向 Codex 的质量优先、上下文高效工作流管理插件：只减少冗余和噪声，不削弱解决问题所需的推理、证据、纠错与验收验证。

## 核心能力

- 正确性与必要推理、证据、纠错和验收验证始终高于 token 或上下文节省。
- 先判断日常问题或工作问题：聊天、天气、日报、电脑清理等日常请求保持当前会话设置；设备定制、设备 Bug、App/代码开发、构建部署及工程诊断进入工作评估。
- 每个 Work 新目标显式创建一个最高可用模型/推理的绑定评估子智能体；PreTool 仅接受完整证明请求。Start 的 active model 匹配可证明模型，推理强度只有宿主明确回显才可观测，缺失不算失败。
- 简单工作问题由评估档直接解决和验证，不额外增加计划确认轮次。
- 困难工作问题先只读取证据，再给出包含模块、文件、方法、改动、构建部署、验收、风险和回退的详细计划；只有用户严格确认当前计划后才开始写入、构建或部署。
- 详细困难计划进入待确认状态时会在插件私有数据目录生成经过清理、与目标/难度/代次/摘要绑定的 Markdown 审阅镜像；镜像只便于审阅，不能确认计划或授权执行，状态中的 `plan_digest` 始终是权威绑定。
- 严格确认后，父会话继续以高推理负责协调和复核；从宿主当时实际暴露的选项中选择最新的较低档 Codex 模型，创建唯一合同执行子智能体并固定 `reasoning_effort=medium`，不硬编码具体模型名。
- Hook 不能切换父会话模型；只有宿主接受带显式 `model` 覆盖且 `fork_turns=none` 或正整数的创建请求，才算子智能体切换证据。
- `execution_contract_id` 同时绑定目标、难度决策、计划代次和计划摘要；失败按类型记录，初次失败后最多允许一次有实质修正的恢复，禁止原样重试。
- 已确认计划执行后封存不含原文的改动/验证基线；同一会话验收发现新问题时，先结合前目标、计划、合同和证据做只读因果复核，再决定重规划、重分类或继续取证。
- 新增约束、范围或目标会使待确认计划失效并要求重新规划；日常/工作与简单/困难分类都不改变删除、覆盖、安装、外发等安全边界。
- 按任务复杂度选择直接处理、聚焦处理或复杂工作流。
- 复杂任务主动评估关键路径，只要预期节省时间高于协调成本，就优先并行调度独立的读、写、测试、研究或复核工作。
- Complex 最多 2 个、Extensive 最多 3 个子智能体；上限只是容量，不是固定数量，也不要求必须派一个只读子智能体。
- 日常请求保持当前策略；明确“不使用子智能体”会硬关闭评估者并本地处理。唯一执行合同只约束已确认困难计划的变更范围，其他并行通道不得成为第二个执行者。
- 只有用户明确要求“本/整个会话始终使用最高可用模型和最高推理强度”时，才启用跨目标与压缩恢复保留的 `highest_throughout` 会话偏好；普通的“最高模型”、单次任务高推理或日常请求不会触发，明确恢复默认策略才退出。
- 默认策略保持不变：Daily 使用当前策略，Work 使用最高档评估者，困难计划确认后仍由最新较低档、中等推理执行者实施。`highest_throughout` 只把已确认困难计划的执行者请求改为宿主实际可用最高档；Hook 只记录和校验请求及宿主回显，不宣称父会话或子智能体已成功切档。
- 通过明确文件/模块所有权避免写冲突；共享构建服务器、设备或交付物只串行化实际冲突的阶段。
- 跨任务协调只在 fresh `list_threads` 同时证明当前任务与目标任务同宿主、不同任务且均为 active，并且当前证据确认双方争用同一资源的冲突阶段时发送一次；idle、`notLoaded`、已完成、异资源或兼容阶段不通知，普通跨任务消息不受影响。
- 已确认困难计划的绑定执行者真正卡住时，普通首个失败仍由原档修正；只有执行者停止变更并提交精确 stall 证据，才复用原最高档评估者做一次只读诊断。合同内修正恢复卡顿前的执行档，扩大范围则重新规划并严格确认，失败或再次卡顿不会循环升档。
- 子智能体结果返回后，父会话只在宿主仍把该精确代理显示为 running 时停止它；状态按完整生命周期折叠，所有 pending/live/当前绑定代理始终保留，终态历史超过 10 个时只裁最旧完整终态组。Hook 不会伪装成能删除宿主任务或侧边栏历史。
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
py -3 "$CodexHome\plugins\cache\workflow-manager\workflow-manager\1.0.35\scripts\install_stable_skill.py" --codex-home "$CodexHome"
```

Linux、WSL 或 macOS：

```bash
codex_home="${CODEX_HOME:-$HOME/.codex}"
python3 "$codex_home/plugins/cache/workflow-manager/workflow-manager/1.0.35/scripts/install_stable_skill.py" --codex-home "$codex_home"
```

检查安装状态：

```bash
codex plugin list --json
```

同步成功时会输出 `"status": "installed"`、`"updated"` 或 `"current"`，目标固定为 `$CODEX_HOME/skills/workflow-manager`。随后重启 Codex 并新建会话，以重新加载 Skills 目录。Hook 会在 `SessionStart` 自动补建或更新稳定副本，但显式同步可以保证安装后的第一条新任务就能发现它。若稳定目录已存在但不带 Workflow Manager 受管标记，安装器会拒绝覆盖。

Workflow Manager 不会仅因新版 Hook 已接管就删除旧版本缓存：旧任务仍可能保留原有版本化注入记录。插件不会直接改写 Codex 的 rollout JSONL、SQLite、索引或活动任务文件。若团队策略限制 GitHub 市场，请先让管理员允许该仓库来源。

## Hook 命令信任

插件已安装或已启用，不等于 Codex Desktop 已信任其 Hook 命令。首次启用以及 Hook 命令定义发生变化后，都应在实际运行 Desktop 的宿主中打开 `/hooks` 或 Hooks 设置，审核 Workflow Manager 当前定义；只有企业 `managed` 策略可以由管理员自动信任。插件不会自行写入 `trusted_hash`，也不要启用 `dangerously-bypass-hook-trust` 或任何同类危险 bypass 来跳过审核。

可在与 Desktop 相同的宿主上运行只读检查；`--cwd` 应指向实际使用插件的工作目录，按需增加 `--json`。Windows：

```powershell
$CodexHome = Join-Path $env:USERPROFILE ".codex"
py -3 "$CodexHome\plugins\cache\workflow-manager\workflow-manager\1.0.35\scripts\hook_trust_doctor.py" --cwd "C:\path\to\workspace"
```

Linux、WSL 或 macOS：

```bash
codex_home="${CODEX_HOME:-$HOME/.codex}"
python3 "$codex_home/plugins/cache/workflow-manager/workflow-manager/1.0.35/scripts/hook_trust_doctor.py" --cwd /path/to/workspace
```

`hook_trust_doctor.py` 只调用 app-server 的 `hooks/list`，不会修改配置。退出码：

- `0`：9 个 Hook 均已启用且状态为 `trusted` 或 `managed`。
- `2`：至少一项被禁用或处于 `untrusted`/`modified`，需要回到 Desktop 审核。
- `1`：CLI、app-server、参数、协议或发现错误，本次检查无法得出结论。

Windows 与 WSL 共享配置时，两端的命令定义及其哈希仍可能不同。审核必须以实际运行 Codex Desktop 的宿主所显示的当前定义为准；切换宿主后应重新审核，不要复制或固化旧宿主的哈希。

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

升级后再次运行对应平台的 `install_stable_skill.py` 命令。1.0.28 起 Hook 只执行宿主注入的精确 `PLUGIN_ROOT`；精确缓存缺失时会安全 fail-open，不会接管其他版本、marketplace 或稳定 Skill 中的代码。需要保留旧任务时，请让宿主保留其旧缓存到任务结束；重装或重启后，新任务会从当前精确插件根和无版本 Skill 路径加载。

生产环境可固定到发布标签：

```bash
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.35 --json
```

如需回退，先移除插件和市场，再使用目标标签重新添加：

```bash
codex plugin remove workflow-manager@workflow-manager --json
codex plugin marketplace remove workflow-manager --json
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.13 --json
codex plugin add workflow-manager@workflow-manager --json
```

## 工作方式与边界

处理顺序是“日常/工作 → Work 的高档评估者判简单/困难 → 独立判断 Direct/Focused/Complex/Extensive”。困难不等于增加执行者数量；除高评估者和已确认的唯一执行者外，子智能体数量也不能反向决定问题难度。任务仍只按需经过 `Contract → Evidence → Change → Verify → Report`。

困难计划等待确认期间，目标读取、搜索、静态检查、计划更新、澄清问题和明确只读的子智能体调查可以继续；明确文件写入、变更型子智能体或 Git、构建打包、部署安装和设备变更会被拦截。确认只绑定当前计划、目标和难度判断；任何新增约束或重规划请求都会使原确认失效。

私有 Markdown 镜像只复制经过清理和大小限制的详细计划正文。镜像写入失败、正文漂移或路径身份异常不会改变权威 `plan_digest`，也不会自行锁定或开放确认；修正真实原因后，同一计划可以安全重试写入。每个会话只保留当前镜像和最新 5 个受管旧镜像；保留清理以最多 16 项的有界事务执行，路径绑定或删除前后校验失败时逐字节回滚，符号链接、硬链接、同名竞态和目录替换均以 `unsafe_path` 关闭镜像 I/O。

插件会读取 Codex 生命周期事件来判断路由、输出规模和续接状态；持久化数据只保留摘要、指纹、验收待办状态和计数，不保存原始提示词、命令或子智能体结果。大工具结果会保留给模型正常推理，插件只提示后续查询如何收窄，不会仅因为输出较大而替换必要证据。钩子属于工作流护栏，不是安全边界。子智能体可能减少主会话噪声，但不保证降低总 token 消耗。

1.0.23 增加 `work_executor_low_latest` 逻辑策略：它表示“选择宿主当前实际可用的最新较低档 Codex 模型执行已确认计划”，不是固定产品名。父会话仍以高推理负责合同、协调、恢复决策和最终验收；唯一执行子智能体使用显式模型覆盖、`reasoning_effort=medium`，并在覆盖模型时提供 `fork_turns=none` 或正整数。Hook 不能切换父模型，也不能仅凭状态字段证明子智能体切换；只有宿主接受该显式创建请求才是切换证据。若没有合格模型，必须报告类型化的 `model_unavailable`，不能静默回退或虚构模型标识。

执行合同由目标指纹、难度决策 ID、正数计划代次与已确认计划摘要共同生成。创建请求还必须带完整可执行计划、独占范围、验收与回退。初次执行失败会记录为模型、配置、创建、启动匹配、合同过期、实现、构建、部署或验证等类型；仅在修正对应原因后允许一次恢复，总尝试最多两次。第二次失败或没有实质修正时停止并交回父会话重新评估，禁止换一种命令写法原样重试。

合同执行完成后，Schema 11 会封存上一次目标、计划、合同、改动和后续验证的有界指纹基线。用户在同一会话验收发现遗留、复现或新症状时，表述只会触发只读复核，不会直接被当作因果证据。`introduced` 或 `fix_ineffective` 会要求整体重规划和再次确认，`unrelated` 会脱离旧合同重新分类，`uncertain` 会保持只读并继续取得缺失证据。

压缩和恢复只携带基线/复核 ID、指纹、摘要和枚举，不保存用户原话或计划正文。Schema 10 迁移到 Schema 11 时不会猜测用户验收状态或自行生成因果结论。

如果执行者结束但没有记录任何成功改动，随后又未通过验收，系统不会伪造“前序改动引入问题”的因果结论，也不会继续复用旧成功合同；它会标记验收失败，回到高推理分析并重新给出待确认的完整计划。

## 仓库结构

```text
.agents/plugins/marketplace.json       GitHub 插件市场
plugins/workflow-manager/              插件源码
  .codex-plugin/plugin.json            插件清单
  assets/stable-skill/workflow-manager/  唯一可调用 Skill 的安装源
    references/work-routing.md          困难判断、计划确认与防误拦边界
    references/confirmed-execution.md   合同执行、模型证据、失败恢复与迁移
    references/regression-continuity.md 验收回归、因果复核、重规划与压缩续接
    references/live-coordination.md      实时跨任务共享资源协调与隐私边界
    references/stall-recovery.md         卡顿升档诊断、原档恢复与防循环边界
    references/agent-lifecycle.md        子智能体终止、代次关联与安全裁剪边界
  hooks/hooks.json                     生命周期钩子
  scripts/generate_hook_commands.py    九个 Hook 事件的命令单一生成源
  scripts/install_stable_skill.py      用户级稳定路径安装器
  scripts/                             其他跨平台运行脚本
  tests/                               策略与 Windows 原生测试
```

## 开发与测试

仓库一致性检查：

```bash
python plugins/workflow-manager/scripts/generate_hook_commands.py --check
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
py -3 -m unittest -v tests.test_plan_artifact
```

提交前应同时通过仓库校验、完整 Python 测试、Windows 14 项 Hook 测试和 30 项计划镜像测试；无符号链接权限时仅允许对应夹具精确跳过。GitHub Actions 会自动执行这些检查。

## 贡献与发布

行为修改请附最小复现和验证结果，避免上传原始会话、完整日志、密钥或设备隐私数据。发布时同步更新插件版本与 [CHANGELOG.md](CHANGELOG.md)，通过测试后创建同版本 `vX.Y.Z` 标签。

安装命令依据 OpenAI 官方的 [插件打包与 GitHub 市场说明](https://developers.openai.com/plugins/build/plugins)。项目采用 [MIT License](LICENSE)。
