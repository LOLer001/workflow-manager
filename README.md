# Workflow Manager

面向 Codex 的质量优先、上下文高效工作流管理插件：只减少冗余和噪声，不削弱解决问题所需的推理、证据、纠错与验收验证。

## 核心能力

1.0.48 将 Workflow Manager 收敛为当前 Codex 的窄授权与宿主证据层：高置信 Simple 保持 `Start=0`；Hard 使用动态 manifest 预算、可连续恢复的单调 sequence、仅摘要的授权 envelope 与三层 lifecycle 真值，不再用固定切片/尝试数或墙钟超时替代诊断。

- 当前 Codex 原生负责普通规划、并行、进度、工具使用、常规错误恢复、压缩、模型选择和子智能体编排；插件不再重复注入 route、阶段顺序、agent cap、side lane、压力、重试或通用进度建议。
- 固定门禁只保留四类不可替代边界：宿主 request/acceptance/Start 真值、Hard 授权 envelope、单 live writer/禁止 child nesting，以及挂载树 Git/破坏性外部动作安全。计划措辞、列表格式、切片数量、阶段数量、墙钟、普通输出形态和原生调度都不是独立门禁。
- Daily 与全部非 Hard Work 直接使用原生 Codex，不启动 Simple assessor。只有高置信证据证明单个关键风险，或至少两个独立强信号组且其中包含未知根因、跨范围或连续性时才进入 Hard；设备、构建部署、阶段数量、共享资源、长度和含糊文字本身都不够。
- 新 Hard 目标请求一个最高可用模型、`reasoning_effort=max`、`fork_turns=1` 的只读评估者；只有显式 `highest_throughout` 使用 `ultra`。评估者只交付宿主绑定的原生分析，parent 负责写入唯一 canonical 详细计划并等待严格确认；不再要求 child 复述插件 marker 或 manifest DSL。
- assessor 活性只接受当前 binding、agent、sequence 的新进度摘要；600 秒只观察，1200 秒仍为 live，严格超过 1200 秒无进展时诊断、解卡或拆分当前步骤，不直接写成 `assessment_timeout`、`blocked` 或 exhausted。轮询、迟到事件和压缩恢复不会伪造进展。
- parent 直接写普通人类可读计划，不要求 JSON、fence、关键词或固定尾句；未提供 manifest 时完整计划就是一个逻辑 slice，显式 manifest 可在 196608-byte / 1024-node 总预算内动态扩展且不设独立 slice/list 上限。3–5 slice 只是模型的常见选择。默认 executor 使用较低档模型、`medium`、`fork_turns=1`；显式 `highest_throughout` 才使用最高档 `ultra`。
- 所有 failure/stall/incomplete/verification typed recovery 请求当前最高可用 `gpt-5.6-sol`、`max`、`fork_turns=1`。PreTool 唯一 request、PostTool 显式 `host_accepted=true` 与唯一 full Start 必须和扁平状态同时核对 binding/objective/sequence/model/effort/fork；缺失、未知或拒绝 host acceptance 为 `model_unavailable`，其余冲突为 `start_mismatch`，不得从扁平字段或 child 自报推断。
- 当前宿主无通用 status 的成功 spawn 以精确 canonical `task_name` 结构化 Post 回执证明；首个回执唯一绑定、相同重复幂等、冲突不可升级。Start/Post 乱序时 executor 只接收 locked current-slice handoff，journal 在解锁点再次验证，hand-off 本身不提前授予写权限。
- 私有 Start handoff 从可信状态交付宿主签发的合同与当前计划；executor 不读取插件状态，也不从 request prose 或 task name 猜授权。安全 ASCII `task_name` 只是宿主标签，不编码 contract/slice/sequence/fingerprint。recovery 使用受状态字节/节点预算保护的单调 sequence与仅摘要预约；相同失败指纹且无新证据的原样重放会被原子拒绝，有新证据、根因、实质修正或不同 fingerprint 可继续，三次以上不同 fingerprint 不会按固定 attempt 耗尽。terminal child 不复活、不嵌套，始终只有一个 live writer。
- 严格确认只绑定归一化的 objective、显式 acceptance、risk category 与 irreversible external action digest，不绑定计划 prose、slice 布局或 manifest digest。同 envelope 的 repair、autosplit、verification 和 compaction successor 自动继承确认；只有上述授权字段实质变化才重确认。
- assessor 已完成但 parent Stop 尚未提交可信 revision 时，提前到达的纯确认只持久化 host-bound receipt digest，保留 pending plan、Hard binding 与 repair；可信 revision 落盘后自动绑定确认，不重置为 Daily，也不要求用户重发。
- 兼容当前 Codex 在同一 turn 生成多个独立 `exec`、JSON 参数形式 `tools.exec_command({...})`；补丁恢复兼容 legacy 唯一成功 `patch_apply_end` 与当前精确空 `{}` 成功回执，多补丁必须按唯一 call id、唯一字面摘要、唯一回执及宿主 operation 顺序一一绑定，重复、额外 sibling、拒绝或歧义链仍保持未知。
- Desktop 的 Stop 若遗漏父审正文或终态，不再把已完成的强验收降成 `verification_failed`：同 session rollout 中唯一精确末行、完全一致的 `task_complete.last_agent_message`、逐条唯一且全部成功的 bound verification、无后续 mutation 和原 contract/slice 同时成立时，resume 会直接封存既有 review；否则保持失败。已通过切片的 child 终态遗漏也只在 full Start、唯一 owner、精确 succeeded result、已封存父审和无冲突写错误同时成立时补齐 change evidence。
- canonical journal、目标/难度/代次/摘要、执行合同、切片 token、宿主生成的候选证据与父审证据共同构成授权。外部漂移、错执行者、错切片、终态复活或降级验收一律 fail-closed。
- 已复现、范围内且可回滚的问题必须进入最早当前 ownership 直接修复；只有目标变化、危险/不可恢复状态、缺少必要授权或强验收失败才停止。短 delegated recovery 保留 Work/Hard 路由；带说明的 replan 语句不再依赖窄词表。
- 已封存合同的状态/证据查询保持 continuation，不会重新打开 assessor；英文 `add/remove/change` 仅按独立单词识别，repair 字段名或协议标识符中的 `_change_` 不再被误判为修改计划。显式的 `change the acceptance scope` 仍会正常失效旧授权。
- bound assessor 的唯一 request、精确 Post 回执、full Start 和非空终态结果是评估身份权威；parent 的非空、总预算内可信 canonical revision 是唯一计划内容权威。`WORK_ASSESSMENT`、fence、固定关键词、固定尾句、最小 prose 字节数、任务名编码和任意列表格式全部不是门禁；模型自行判断内容质量与计划结构。
- executor 与 parent review 均可返回普通非空原生 prose；`EXECUTION_RESULT`/`EXECUTION_REVIEW` 是可选兼容标记，只有显式畸形或冲突意图才拒绝。宿主记录的真实 verification 与独立 parent verification 决定是否通过；Desktop 仅遗漏通用 status 时，精确绑定 executor/contract/slice/input/command/turn 的 PostToolUse 改动仍可由独立父审封存，不必为格式缺失再启动 recovery child。没有真实 verification 才进入 `incomplete_execution`，强验收不降级。
- confirmed executor 的长测从启动时就必须处于 foreground deadline，保存 wrapper outer status 和 underlying inner exit 并收口进程组；自身引入的可修复命令合同错误在同一 live owner 内修正重跑。
- 稳定 Skill 使用逐文件摘要做精确同步：版本中已退休且仍与历史发布字节一致的旧引用会被删除，用户新增或修改的文件、链接和未知内容不会被覆盖或误删，避免新版仍隐式加载旧 agent lifecycle/协调流程。
- 普通压缩恢复只依赖原生摘要；活动 confirmed Hard 会在内部完整校验 canonical 当前修订，但只向模型投影当前 slice、global constraints、合同/attempt/父审状态与已完成前缀摘要，不重放完整计划。尚待确认、重规划、因果复核或参考验收才按对应边界恢复所需语义。
- 固定保留挂载树 Git 边界：不得从 CIFS、Samba、UNC、DrvFS 或其他挂载工作树运行 Git，必须转到权威 native Linux/远端 Git。
- 1.0.45 的通用跨任务协调协议、route/phase/cap、70% 压力提示、普通失败升档和通用 subagent lifecycle 已删除；旧 Schema 26 状态升级时丢弃这些字段，但保留有效 Hard journal 与封存证据。

## 30 秒安装

需要已登录的 Codex CLI。先添加 GitHub 插件市场，再安装插件：

```bash
codex plugin marketplace add LOLer001/workflow-manager --json
codex plugin add workflow-manager@workflow-manager --json
```

安装命令完成后，把插件内的 Skill 资产同步到不含版本号的用户级稳定路径。Windows：

```powershell
$CodexHome = Join-Path $env:USERPROFILE ".codex"
py -3 -B "$CodexHome\plugins\cache\workflow-manager\workflow-manager\1.0.48\scripts\install_stable_skill.py" --codex-home "$CodexHome"
```

Linux、WSL 或 macOS：

```bash
codex_home="${CODEX_HOME:-$HOME/.codex}"
python3 -B "$codex_home/plugins/cache/workflow-manager/workflow-manager/1.0.48/scripts/install_stable_skill.py" --codex-home "$codex_home"
```

检查安装状态：

```bash
codex plugin list --json
```

同步成功时会输出 `"status": "installed"`、`"updated"` 或 `"current"`，目标固定为 `$CODEX_HOME/skills/workflow-manager`。随后重启 Codex 并新建会话，以重新加载 Skills 目录。Hook 会在 `SessionStart` 自动补建或更新稳定副本，但显式同步可以保证安装后的第一条新任务就能发现它。若稳定目录已存在但不带 Workflow Manager 受管标记，安装器会拒绝覆盖。

当无版本稳定 Skill 已验证能覆盖新任务和恢复任务时，1.0.46 会清理严格更旧、确认为 Workflow Manager 自有且不再需要的版本缓存与 bytecode；更高版本、非版本目录、符号链接及无法证明安全的条目一律保留并报告。插件不会直接改写 Codex 的 rollout JSONL、SQLite、索引或活动任务文件；旧任务的 sealed host evidence 按迁移边界保留，而不是伪造成 v10 执行。若团队策略限制 GitHub 市场，请先让管理员允许该仓库来源。

## Hook 命令信任

插件已安装或已启用，不等于 Codex Desktop 已信任其 Hook 命令。首次启用以及 Hook 命令定义发生变化后，都应在实际运行 Desktop 的宿主中打开 `/hooks` 或 Hooks 设置，审核 Workflow Manager 当前定义；只有企业 `managed` 策略可以由管理员自动信任。插件不会自行写入 `trusted_hash`，也不要启用 `dangerously-bypass-hook-trust` 或任何同类危险 bypass 来跳过审核。

可在与 Desktop 相同的宿主上运行只读检查；`--cwd` 应指向实际使用插件的工作目录，按需增加 `--json`。Windows：

```powershell
$CodexHome = Join-Path $env:USERPROFILE ".codex"
py -3 -B "$CodexHome\plugins\cache\workflow-manager\workflow-manager\1.0.48\scripts\hook_trust_doctor.py" --cwd "C:\path\to\workspace"
```

Linux、WSL 或 macOS：

```bash
codex_home="${CODEX_HOME:-$HOME/.codex}"
python3 -B "$codex_home/plugins/cache/workflow-manager/workflow-manager/1.0.48/scripts/hook_trust_doctor.py" --cwd /path/to/workspace
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

普通任务的调度完全由当前 Codex 负责；Workflow Manager 只在真正 Hard 的授权边界出现时介入。

## 升级与回退

跟随仓库默认分支升级：

```bash
codex plugin marketplace upgrade workflow-manager --json
codex plugin add workflow-manager@workflow-manager --json
```

升级后再次运行对应平台的 `install_stable_skill.py` 命令。1.0.28 起 Hook 只执行宿主注入的精确 `PLUGIN_ROOT`；精确缓存缺失时会安全 fail-open，不会接管其他版本、marketplace 或稳定 Skill 中的代码。需要保留旧任务时，请让宿主保留其旧缓存到任务结束；重装或重启后，新任务会从当前精确插件根和无版本 Skill 路径加载。

生产环境可固定到发布标签：

```bash
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.48 --json
```

如需回退，先移除插件和市场，再使用目标标签重新添加：

```bash
codex plugin remove workflow-manager@workflow-manager --json
codex plugin marketplace remove workflow-manager --json
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.13 --json
codex plugin add workflow-manager@workflow-manager --json
```

## 工作方式与边界

Hard 计划等待确认期间允许目标读取、搜索、静态检查、澄清和其他只读证据；`update_plan` 只能投影当前 canonical 修订。文件写入、变更型 child、Git 变更、构建、部署和设备变更必须等待严格确认。

Codex Multi-Agent V2 可能在本地 `PreToolUse` 前加密 collaboration `message`。assessor 与 confirmed executor 使用任意安全 ASCII `task_name` 作为不透明宿主标签，并显式请求 `fork_turns=1`；授权来自可信状态与 request/Post/full Start 三层证据，不来自名称编码。旧 terminal executor 的 follow-up 一律拒绝。

当官方 `Bash` Hook 只暴露挂载的 session `cwd` 时，原生 Linux Git 目录必须在命令中以字面量绝对 `git -C` 明示；挂载路径、虚假 `/tmp` 和链式第二个 Git 都会被拒绝。

私有 canonical 日志每个修订最多 `983040` 字节，整个 `hard-plan.md` 最多 `10485760` 字节。日志与状态采用 `marker → journal → state → cleanup` 两阶段事务；外部改动、路径身份异常或摘要漂移会使授权失效。Schema 19 的旧镜像只在可严格验证时迁移。

`work_executor_low_latest` 是默认正常 executor 的动态逻辑策略，不是固定产品名。typed recovery 使用当前最高可用 `gpt-5.6-sol` + `max` + `fork_turns=1`，只有显式 `highest_throughout` 使用最高档 + `ultra`。PreTool 唯一请求、PostTool `host_accepted=true` 与唯一 full Start 分别记录；只有三者、扁平状态和当前合同一致时才进入 running。每个切片候选仍须通过父会话独立验收，恢复不能复活 terminal child、嵌套 child 或扩大范围。

升级到 Schema 28 时继续清除旧的通用 route/phase/cap、压力、普通失败升档、跨任务协调和普通 agent lifecycle 字段。canonical journal v2、有效 Hard 修订、切片、宿主生成证据、封存 baseline、因果复核和参考验收继续按严格边界迁移；普通规划、并行和压缩语义由当前 Codex 原生恢复。

真实 token 无法从累计宿主计数中可靠归因给插件。本仓库把 1.0.43 的匿名冻结 trace 作为正式静态 A/B：确定性断言 child Start 从 8 降至 3（62.5%，要求至少 60%）、重复 `additionalContext` 从 856 降至 153 UTF-8 字节（82.1%，要求至少 50%），同时保留同等数量的 Hard executor 强验收检查点。另一个三臂模拟只比较 Hook `additionalContext` 字节、child Start、工具尝试和相同验收证据。插件为 Hard 授权增加的 assessor/executor 成本不会伪装成原生零开销；这些模拟字节和累计宿主计数都不是可归因的真实 token。

## 仓库结构

```text
.agents/plugins/marketplace.json       GitHub 插件市场
plugins/workflow-manager/              插件源码
  .codex-plugin/plugin.json            插件清单
  assets/stable-skill/workflow-manager/  唯一可调用 Skill 的安装源
    references/work-routing.md          困难判断、计划确认与防误拦边界
    references/confirmed-execution.md   合同执行、模型证据、失败恢复与迁移
    references/regression-continuity.md 验收回归、因果复核、重规划与压缩续接
    references/stall-recovery.md         卡顿升档诊断、原档恢复与防循环边界
  hooks/hooks.json                     生命周期钩子
  scripts/generate_hook_commands.py    九个 Hook 事件的命令单一生成源
  scripts/install_stable_skill.py      用户级稳定路径安装器
  scripts/                             其他跨平台运行脚本
  tests/                               策略与 Windows 原生测试
```

## 开发与测试

仓库一致性检查：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B plugins/workflow-manager/scripts/generate_hook_commands.py --check
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/validate_repository.py
```

Linux、WSL 或 macOS：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s plugins/workflow-manager/tests -p "test_*.py" -v
```

Windows 原生测试：

```powershell
Set-Location plugins/workflow-manager
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONUTF8 = "1"
py -3 -B -m unittest discover -s tests -p "test_*.py" -v
```

提交前应同时通过仓库校验、完整 Python 测试、Windows 原生 Hook 套件和完整计划日志套件；无符号链接权限时仅允许对应夹具精确跳过，不能掩盖产品失败。GitHub Actions 会自动执行这些检查。

## 贡献与发布

行为修改请附最小复现和验证结果，避免上传原始会话、完整日志、密钥或设备隐私数据。发布时同步更新插件版本与 [CHANGELOG.md](CHANGELOG.md)，通过测试后创建同版本 `vX.Y.Z` 标签。

安装命令依据 OpenAI 官方的 [插件打包与 GitHub 市场说明](https://developers.openai.com/plugins/build/plugins)。项目采用 [MIT License](LICENSE)。
