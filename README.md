# Workflow Manager

面向 Codex 的质量优先、上下文高效工作流管理插件：只减少冗余和噪声，不削弱解决问题所需的推理、证据、纠错与验收验证。

## 核心能力

- 正确性与必要推理、证据、纠错和验收验证始终高于 token 或上下文节省。
- 先判断日常问题或工作问题：聊天、天气、日报、电脑清理等日常请求保持当前会话设置；设备定制、设备 Bug、App/代码开发、构建部署、工程诊断，以及明确创建并验证文件产物的工程验收合同进入工作评估。
- 高置信 Simple/Focused Work 保持本地、真实 child Start=0。只有命中单个关键风险，或至少两个独立强信号组且其中包含诊断、范围、连续性之一，才认定 Hard；设备、构建、三阶段、共享资源或文字含糊本身都不构成 Hard。Hard 评估默认请求最高可用模型与 `max`，仅显式“本会话全程最高”改为 `ultra`。
- 同时明确声明“身份/激活预检、禁用 tool、禁用 child”的宿主本地探针固定为 Daily/direct、child Start=0；即使探针文字提到 Work/Hard 或 Workflow Manager，也不得升级为工程评估。若它替换了一个被中断且仍显示 running 的错误预检，状态会留下归一化 guard 后清除陈旧绑定。
- `identity_evidence.plugin_root_fingerprint` 是当前处理事件的 Hook 身份，而不是历史会话属性；每个成功持久化的当前事件都会用实际 `PLUGIN_ROOT` 刷新它，cachebuster/reload 后不得继续回显旧缓存。
- 简单工作问题由评估档直接解决和验证，不额外增加计划确认轮次。
- 困难工作问题先只读取证据，再给出包含模块、文件、方法、改动、构建部署、验收、风险和回退的详细计划；只有用户严格确认当前计划后才开始写入、构建或部署。
- 详细困难计划只有在成功追加到插件私有 `plans/<session-token>/hard-plan.md` 后才能进入待确认；同一会话的每次完整修订都追加到这个固定 canonical Markdown，当前受信修订定义计划内容。
- 每个新版困难计划修订末尾都带唯一 `workflow-manager-execution-slices` JSON manifest；1 到 6 个连续 `sNN` 切片分别绑定范围、验收、回退、停止条件与产物，正常为 1–3 个，只在真实依赖或风险边界需要时拆分。
- canonical Markdown 本身绝不确认计划或授权执行；状态中的目标/难度/代次及修订与全文摘要负责验证和授权绑定，外部改动会立即使计划与执行合同失效。
- 严格确认后，父会话继续以高推理负责协调和复核；从宿主当时实际暴露的选项中选择最新的较低档 Codex 模型，创建唯一合同执行子智能体并固定 `reasoning_effort=medium`，不硬编码具体模型名。
- Hook 不能切换父会话模型；PreTool 只记录 `requested`，PostToolUse 成功/失败独立记录 `host_accepted`，Start 再记录 `full|partial|absent|mismatch`。bound child 只有 host accepted 且 Start 完整匹配才可运行；Start `model` 只取官方 Hook payload，effort 只取同一 turn 的宿主 transcript context，绝不从请求值或 child 自报补造。
- `execution_contract_id` 同时绑定目标、难度决策、计划代次和计划摘要。首个普通失败要求原执行者立即做一次实质修正并继续；根因未知、风险关键或修正仍失败时，主动调用最高可用模型与 `max` 做一次绑定诊断，再把最小修正交回原执行路线。只有真实外部阻塞才停止。
- 当前切片执行者的严格末行 `EXECUTION_RESULT execution_contract_id=<32hex> slice_id=sNN outcome=succeeded|failed` 只进入候选态；父会话必须独立验收并以严格末行 `EXECUTION_REVIEW execution_contract_id=<32hex> slice_id=sNN outcome=passed|failed` 才能推进。两种 marker 都不再由模型自报 evidence digest；Hook 从合同、切片、尝试与完整结果生成并规范化证据。每片最多一次 fresh v2，只有最后一片通过才封存全局成功。
- 已确认计划执行后封存不含原文的改动/验证基线；同一会话验收发现新问题时，先结合前目标、计划、合同和证据做只读因果复核，再决定重规划、重分类或继续取证。
- 新增约束、范围或目标会使待确认计划失效并要求重新规划；日常/工作与简单/困难分类都不改变删除、覆盖、安装、外发等安全边界。
- 按任务复杂度选择直接处理、聚焦处理或复杂工作流。
- 复杂任务主动评估关键路径，只要预期节省时间高于协调成本，就优先并行调度独立的读、写、测试、研究或复核工作。
- Complex 最多 2 个、Extensive 最多 3 个子智能体；上限只是容量，不是固定数量，也不要求必须派一个只读子智能体。
- 日常请求保持当前策略；明确“不使用子智能体”会硬关闭评估者并本地处理。唯一全局合同和至多一个当前切片执行者约束已确认困难计划的变更范围，其他并行通道不得成为第二个执行者。
- 只有用户明确要求“本/整个会话始终使用最高可用模型和最高推理强度”时，才启用跨目标与压缩恢复保留的 `highest_throughout` 会话偏好；普通的“最高模型”、单次任务高推理或日常请求不会触发，明确恢复默认策略才退出。
- 默认策略为：Daily 使用当前策略，Work 的计划评估使用最高模型加第二高推理档 `max`，困难计划确认后仍由最新较低档、中等推理执行者实施。`highest_throughout` 把评估和已确认困难计划执行者都请求为宿主最高档；Hook 只记录请求及宿主回显，不宣称父会话或子智能体已成功切档。
- 通过明确文件/模块所有权避免写冲突；共享构建服务器、设备或交付物只串行化实际冲突的阶段。
- 跨任务协调只在 fresh `list_threads` 同时证明当前任务与目标任务同宿主、不同任务且均为 active，并且当前证据确认双方争用同一资源的冲突阶段时发送一次；idle、`notLoaded`、已完成、异资源或兼容阶段不通知，普通跨任务消息不受影响。
- 已确认困难计划的绑定执行者真正卡住时，普通首个失败仍由原档修正；只有执行者停止变更并提交精确 stall 证据，才复用原最高档评估者做一次只读诊断。合同内修正恢复卡顿前的执行档，扩大范围则重新规划并严格确认，失败或再次卡顿不会循环升档。
- 子智能体结果返回后，父会话只在宿主仍把该精确代理显示为 running 时停止它；状态按完整生命周期折叠，所有 pending/live/当前绑定代理始终保留，终态历史超过 10 个时只裁最旧完整终态组。Hook 不会伪装成能删除宿主任务或侧边栏历史。
- 插件只保留四类有授权意义的硬门禁：未确认的 Hard 写入、错执行者/错切片、挂载树 Git 与破坏性/外发边界。大输出、重复只读、阶段动作计数和常规上下文压力只作遥测，不拦截正常解决问题。
- 路由上下文只在新目标、路由改变、授权变化或恢复边界出现时注入；不重复输出压力、阶段预算、重复成功和通用工作建议。
- 压缩后由原生摘要续接非计划状态、由 canonical Markdown 重读当前困难计划，并复用仍然有效的验证结果，不从头重复。

## 30 秒安装

需要已登录的 Codex CLI。先添加 GitHub 插件市场，再安装插件：

```bash
codex plugin marketplace add LOLer001/workflow-manager --json
codex plugin add workflow-manager@workflow-manager --json
```

安装命令完成后，把插件内的 Skill 资产同步到不含版本号的用户级稳定路径。Windows：

```powershell
$CodexHome = Join-Path $env:USERPROFILE ".codex"
py -3 -B "$CodexHome\plugins\cache\workflow-manager\workflow-manager\1.0.45\scripts\install_stable_skill.py" --codex-home "$CodexHome"
```

Linux、WSL 或 macOS：

```bash
codex_home="${CODEX_HOME:-$HOME/.codex}"
python3 -B "$codex_home/plugins/cache/workflow-manager/workflow-manager/1.0.45/scripts/install_stable_skill.py" --codex-home "$codex_home"
```

检查安装状态：

```bash
codex plugin list --json
```

同步成功时会输出 `"status": "installed"`、`"updated"` 或 `"current"`，目标固定为 `$CODEX_HOME/skills/workflow-manager`。随后重启 Codex 并新建会话，以重新加载 Skills 目录。Hook 会在 `SessionStart` 自动补建或更新稳定副本，但显式同步可以保证安装后的第一条新任务就能发现它。若稳定目录已存在但不带 Workflow Manager 受管标记，安装器会拒绝覆盖。

当无版本稳定 Skill 已验证能覆盖新任务和恢复任务时，1.0.45 会清理严格更旧、确认为 Workflow Manager 自有且不再需要的版本缓存与 bytecode；更高版本、非版本目录、符号链接及无法证明安全的条目一律保留并报告。插件不会直接改写 Codex 的 rollout JSONL、SQLite、索引或活动任务文件；旧任务的 sealed host evidence 按迁移边界保留，而不是伪造成 v9 执行。若团队策略限制 GitHub 市场，请先让管理员允许该仓库来源。

## Hook 命令信任

插件已安装或已启用，不等于 Codex Desktop 已信任其 Hook 命令。首次启用以及 Hook 命令定义发生变化后，都应在实际运行 Desktop 的宿主中打开 `/hooks` 或 Hooks 设置，审核 Workflow Manager 当前定义；只有企业 `managed` 策略可以由管理员自动信任。插件不会自行写入 `trusted_hash`，也不要启用 `dangerously-bypass-hook-trust` 或任何同类危险 bypass 来跳过审核。

可在与 Desktop 相同的宿主上运行只读检查；`--cwd` 应指向实际使用插件的工作目录，按需增加 `--json`。Windows：

```powershell
$CodexHome = Join-Path $env:USERPROFILE ".codex"
py -3 -B "$CodexHome\plugins\cache\workflow-manager\workflow-manager\1.0.45\scripts\hook_trust_doctor.py" --cwd "C:\path\to\workspace"
```

Linux、WSL 或 macOS：

```bash
codex_home="${CODEX_HOME:-$HOME/.codex}"
python3 -B "$codex_home/plugins/cache/workflow-manager/workflow-manager/1.0.45/scripts/hook_trust_doctor.py" --cwd /path/to/workspace
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
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.45 --json
```

如需回退，先移除插件和市场，再使用目标标签重新添加：

```bash
codex plugin remove workflow-manager@workflow-manager --json
codex plugin marketplace remove workflow-manager --json
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.13 --json
codex plugin add workflow-manager@workflow-manager --json
```

## 工作方式与边界

处理顺序是“日常/工作 → Work 的高档评估者判简单/困难 → 独立判断 Direct/Focused/Complex/Extensive”。困难不等于增加执行者数量；除高评估者和已确认的当前切片执行者外，子智能体数量也不能反向决定问题难度。任务仍只按需经过 `Contract → Evidence → Change → Verify → Report`。

困难计划等待确认期间，目标读取、搜索、静态检查、澄清问题和明确只读的子智能体调查可以继续；`update_plan` 只能显示与当前 canonical 修订摘要一致的投影，不能独立创作或修改计划。明确文件写入、变更型子智能体或 Git、构建打包、部署安装和设备变更会被拦截。确认只绑定当前受信修订、目标和难度判断；任何新增约束或重规划请求都会追加一份完整新修订并要求再次确认。

Codex Multi-Agent V2 可能在本地 `PreToolUse` 前加密 collaboration `message`。此时 assessor 与 confirmed executor 使用状态派生的可见 ASCII `task_name` 和精确 `fork_turns=1` 绑定请求；Simple follow-up 还必须命中此前已接受的 canonical assessor target。切片 v1 使用 `confirmed_executor_<contract32>_<slice_id>_<slice_token16>_v1`，普通恢复追加当前 failure，父级验收失败的 fresh v2 使用 `..._<slice_id>_<slice_token16>_vf_<host_review_evidence32>_v2`；该 evidence 由 Hook 生成，模型不手工填写。旧 terminal executor 的 follow-up 一律拒绝。加密 stall diagnosis 若缺少可见 stall 合同仍会 fail-closed。

当官方 `Bash` Hook 只暴露挂载的 session `cwd` 时，原生 Linux Git 目录必须在命令中以字面量绝对 `git -C` 明示；每个工具调用只允许一个 Git 操作。挂载路径、虚假 `/tmp` 和链式第二个 Git 仍会被拒绝。

私有 canonical 日志只接受 Hook 清理并绑定的完整计划修订。单次修订最多 `983040` 字节、整个 `hard-plan.md` 最多 `10485760` 字节，恰好达到上限允许写入；超出时分别以 `revision_too_large` 或 `journal_full` 类型化拒绝，文件逐字节不变且代次不增加。查看计划详情、重规划、压缩恢复和执行者都必须重读当前受信修订；任何外部改动、路径身份异常或摘要漂移都会进入 `invalidated`/`stale_contract`，不能靠编辑 Markdown 获得权限。

日志与状态采用 `marker → journal → state → cleanup` 两阶段事务。崩溃恢复只接受旧日志/旧状态或新日志/新状态；其他组合 fail-closed，保留诊断标记。Schema 19 最多迁移 6 个严格可验证的旧镜像，只有 canonical 日志和当前 Schema 26 状态共同提交后才清理旧文件；缺失、漂移、不可解析或超量迁移都不会臆造计划正文或切片 manifest。

插件会读取 Codex 生命周期事件来判断路由、输出规模和续接状态；持久化数据只保留摘要、指纹、验收待办状态和计数，不保存原始提示词、命令或子智能体结果。大工具结果会保留给模型正常推理，插件只提示后续查询如何收窄，不会仅因为输出较大而替换必要证据。钩子属于工作流护栏，不是安全边界。子智能体可能减少主会话噪声，但不保证降低总 token 消耗。

当前 `work_executor_low_latest` 逻辑策略表示“选择宿主当前实际可用的最新较低档 Codex 模型执行已确认计划”，不是固定产品名。父会话仍以高推理负责合同、协调、恢复决策和最终验收；每次至多一个当前切片执行子智能体使用显式模型覆盖、`reasoning_effort=medium` 和精确 `fork_turns=1`。Hook 不能切换父模型，也不能仅凭状态字段证明子智能体切换；只有宿主接受该显式创建请求才是切换证据。若没有合格模型，必须报告类型化的 `model_unavailable`，不能静默回退或虚构模型标识。

全局执行合同由 profile、目标指纹、难度决策 ID、正数计划代次、canonical 相对路径、当前修订/全文摘要和 slice manifest 摘要共同生成；当前 slice token 还绑定切片内容、顺序和已验收前缀。该相对路径只是在插件数据根内定位的合同元数据，绝不能按 `cwd` 或 workspace 解析。匹配的 executor `SubagentStart` 必须先验证 journal/manifest，再由 Hook 私下交付全局约束和 exact current slice；读取失败、摘要漂移、错误 token 或跳片都不会进入 running。每片初次失败后仅在实质修正对应原因时允许一个 fresh v2，总尝试最多两次；第二次失败或没有修正时停止并交回父会话，禁止 follow-up 复活 terminal executor 或换一种命令写法原样重试。

升级边界不会把旧结果伪装成新协议：Schema 23 已有 passed baseline 的 sealed v6 成功保留原 profile/contract；v6 `verification_required` 候选只能延续一次只读父审，旧 marker 中的 32 位 digest 只作兼容语法并由 Hook 忽略后重新规范化。其他缺少合法 manifest 的活动 v6 状态 fail-closed，`spawn_pending`/`running` 不获得 v9 写权，必须向同一个 Markdown 追加完整 manifest 修订、重新严格确认并生成新合同；失败复核不能重置为两次新恢复。

所有切片都经父会话独立验收后，系统才封存上一次目标、计划、合同、切片完成链、改动和验证的有界指纹基线。用户在同一会话验收发现遗留、复现或新症状时，表述只会触发只读复核，不会直接被当作因果证据。`introduced` 或 `fix_ineffective` 会要求整体重规划和再次确认，`unrelated` 会脱离旧合同重新分类，`uncertain` 会保持只读并继续取得缺失证据。

压缩和恢复只携带基线/复核 ID、指纹、摘要和枚举，不在状态中复制用户原话或计划正文；困难计划语义始终从受信 canonical 修订恢复。旧 Schema 迁移不会猜测用户验收状态或自行生成因果结论。

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

提交前应同时通过仓库校验、完整 Python 测试、Windows 原生 Hook 套件和完整计划日志套件；无符号链接权限时仅允许对应夹具精确跳过，不能掩盖产品失败。GitHub Actions 会自动执行这些检查。

## 贡献与发布

行为修改请附最小复现和验证结果，避免上传原始会话、完整日志、密钥或设备隐私数据。发布时同步更新插件版本与 [CHANGELOG.md](CHANGELOG.md)，通过测试后创建同版本 `vX.Y.Z` 标签。

安装命令依据 OpenAI 官方的 [插件打包与 GitHub 市场说明](https://developers.openai.com/plugins/build/plugins)。项目采用 [MIT License](LICENSE)。
