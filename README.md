# Workflow Manager

面向 Codex 的质量优先、上下文高效工作流管理插件：只减少冗余和噪声，不削弱解决问题所需的推理、证据、纠错与验收验证。

## 核心能力

1.0.46 把 Workflow Manager 收敛为当前 Codex 之上的窄授权层，而不是第二套任务执行器。

- 当前 Codex 原生负责普通规划、进度、工具使用、错误恢复、压缩、模型选择和子智能体编排；插件不再重复注入 route、阶段顺序、agent cap、side lane、压力、重试或通用进度建议。
- Daily 与全部非 Hard Work 直接使用原生 Codex，不启动 Simple assessor。Hard 只有在单个关键风险，或至少两个独立强信号组且其中包含未知根因、跨范围或连续性时成立；设备、构建部署、阶段数量、共享资源、长度和含糊文字本身都不够。
- 新 Hard 目标请求一个最高可用模型、`reasoning_effort=max`、`fork_turns=1` 的只读评估者；只有显式 `highest_throughout` 使用 `ultra`。评估者写入 canonical 详细计划并等待严格确认。
- 每个 Hard 修订使用 1–6 个执行切片，正常 1–3。确认后默认请求最新可用较低档模型、`medium`、`fork_turns=1` 的唯一当前切片执行者；父会话独立复核后才能推进。
- PreTool 的 `requested`、PostTool 的 `host_accepted` 与 Start 的 `full|partial|absent|mismatch` 分开记录。Start model 只来自官方 Hook payload，effort 只来自同 turn 宿主 transcript；bound Stop 记录这些事实，并在父任务首次 wait/list 收割边界精确回传，`Start=full` 时不得再误报为不可用。缺失、冲突或 child 自报都不能生成运行时授权。
- 兼容当前 Codex 在同一 turn 生成多个独立 `exec`、JSON 参数形式 `tools.exec_command({...})` 与 `item_completed/FileChange` 的宿主事件；按 call id、命令/补丁摘要、当前 attempt、contract 和 slice 逐链核对，重复或歧义链仍保持未知。
- Desktop 的 Stop 若遗漏父审正文或终态，不再把已完成的强验收降成 `verification_failed`：同 session rollout 中唯一精确末行、完全一致的 `task_complete.last_agent_message`、逐条唯一且全部成功的 bound verification、无后续 mutation 和原 contract/slice 同时成立时，resume 会直接封存既有 review；否则保持失败。已通过切片的 child 终态遗漏也只在 full Start、唯一 owner、精确 succeeded result、已封存父审和无冲突写错误同时成立时补齐 change evidence。
- canonical journal、目标/难度/代次/摘要、执行合同、切片 token、宿主生成的候选证据与父审证据共同构成授权。外部漂移、错执行者、错切片、终态复活或降级验收一律 fail-closed。
- 已复现、范围内且可回滚的问题必须进入最早当前 ownership 直接修复；只有目标变化、危险/不可恢复状态、缺少必要授权或强验收失败才停止。短 delegated recovery 保留 Work/Hard 路由；带说明的 replan 语句不再依赖窄词表。
- 已封存合同的状态/证据查询保持 continuation，不会重新打开 assessor；英文 `add/remove/change` 仅按独立单词识别，repair 字段名或协议标识符中的 `_change_` 不再被误判为修改计划。显式的 `change the acceptance scope` 仍会正常失效旧授权。
- bound assessor 的宿主 Start/Stop 与严格计划结构是身份权威；缺少冗余 `WORK_ASSESSMENT` 行或把唯一尾部 manifest 标成普通 `json` 时，Hook 生成摘要并安全规范化，而不是再次启动最高模型只改格式。
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
py -3 -B "$CodexHome\plugins\cache\workflow-manager\workflow-manager\1.0.46\scripts\install_stable_skill.py" --codex-home "$CodexHome"
```

Linux、WSL 或 macOS：

```bash
codex_home="${CODEX_HOME:-$HOME/.codex}"
python3 -B "$codex_home/plugins/cache/workflow-manager/workflow-manager/1.0.46/scripts/install_stable_skill.py" --codex-home "$codex_home"
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
py -3 -B "$CodexHome\plugins\cache\workflow-manager\workflow-manager\1.0.46\scripts\hook_trust_doctor.py" --cwd "C:\path\to\workspace"
```

Linux、WSL 或 macOS：

```bash
codex_home="${CODEX_HOME:-$HOME/.codex}"
python3 -B "$codex_home/plugins/cache/workflow-manager/workflow-manager/1.0.46/scripts/hook_trust_doctor.py" --cwd /path/to/workspace
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
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.46 --json
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

Codex Multi-Agent V2 可能在本地 `PreToolUse` 前加密 collaboration `message`。assessor 与 confirmed executor 因而使用状态派生的可见 ASCII `task_name` 和 `fork_turns=1` 绑定；这只服务插件合同角色，不接管普通原生子智能体。旧 terminal executor 的 follow-up 一律拒绝。

当官方 `Bash` Hook 只暴露挂载的 session `cwd` 时，原生 Linux Git 目录必须在命令中以字面量绝对 `git -C` 明示；挂载路径、虚假 `/tmp` 和链式第二个 Git 都会被拒绝。

私有 canonical 日志每个修订最多 `983040` 字节，整个 `hard-plan.md` 最多 `10485760` 字节。日志与状态采用 `marker → journal → state → cleanup` 两阶段事务；外部改动、路径身份异常或摘要漂移会使授权失效。Schema 19 的旧镜像只在可严格验证时迁移。

`work_executor_low_latest` 是动态逻辑策略，不是固定产品名。PreTool 请求、PostTool 宿主接受与 Start 完整回显分别记录；只有三者和当前合同一致时才进入 running。每个切片候选还要通过父会话独立验收，失败恢复不能复活 terminal v1 或扩大范围。

升级到 Schema 27 时，Schema 26 的通用 route/phase/cap、压力、普通失败升档、跨任务协调和普通 agent lifecycle 字段会被丢弃。有效 canonical Hard 修订、切片、宿主生成证据、封存 baseline、因果复核和参考验收继续按严格边界迁移；普通压缩语义由原生摘要恢复。

真实 token 无法从累计宿主计数中可靠归因给插件。本仓库的三臂模拟只比较确定性 Hook `additionalContext` 字节、child Start 数、工具尝试和相同验收证据：固定困难样例从 1.0.45 的 1489 UTF-8 字节降至 1.0.46 的 543（约 63.5%）。相对冻结的 1.0.43 混合样例，child Start 从 8 降至 3（62.5%），重复 `additionalContext` 从 856 降至 153 UTF-8 字节（82.1%）。无插件/1.0.45/1.0.46 困难三臂仍分别是 1/4/3 次 child Start；插件为 Hard 授权多出的 assessor/executor 成本不会伪装成原生零开销，所有模拟字节也绝不冒充真实 token。

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
