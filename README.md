# Workflow Manager

面向 Codex 的质量优先、上下文高效工作流管理插件：只减少冗余和噪声，不削弱解决问题所需的推理、证据、纠错与验收验证。

## 核心能力

Workflow Manager 1.0.66 是当前 Codex 的 Hard 任务安全层：普通开发保持原生体验，只在高风险、跨范围或未知根因任务中介入。

- **确认后执行**：Hard 任务先只读分析并生成可审阅计划，用户明确确认后才允许修改、构建、部署或设备操作。
- **可信授权**：同时校验子任务请求、宿主接受和实际启动，避免错误模型、错任务或伪造状态获得写权限。
- **原生表达**：计划、执行结果和复核使用普通有界文本；时钟、固定 marker、关键词、结尾和 JSON fence 不构成授权门禁。
- **单写者与独立验收**：同一合同在任一时刻恰好只有一个写入者；无子写入者时父会话可取得当前 slice 租约，子写入者被预留、存活或终态未知时父级写入安全拒绝。
- **连续恢复**：失败、中断、压缩或恢复后沿用已确认范围；原样重放、状态漂移和越权变更会被拒绝。
- **开发环境安全**：阻止在 CIFS、Samba、UNC、DrvFS 等挂载树运行 Git，并对不可逆外部操作保持显式授权。

## 30 秒安装

需要已登录的 Codex CLI。先添加 GitHub 插件市场，再安装插件：

```bash
codex plugin marketplace add LOLer001/workflow-manager --json
codex plugin add workflow-manager@workflow-manager --json
```

安装命令完成后，把插件内的 Skill 资产同步到不含版本号的用户级稳定路径。Windows：

```powershell
$CodexHome = Join-Path $env:USERPROFILE ".codex"
py -3 -B "$CodexHome\plugins\cache\workflow-manager\workflow-manager\1.0.66\scripts\install_stable_skill.py" --codex-home "$CodexHome"
```

Linux、WSL 或 macOS：

```bash
codex_home="${CODEX_HOME:-$HOME/.codex}"
python3 -B "$codex_home/plugins/cache/workflow-manager/workflow-manager/1.0.66/scripts/install_stable_skill.py" --codex-home "$codex_home"
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
py -3 -B "$CodexHome\plugins\cache\workflow-manager\workflow-manager\1.0.66\scripts\hook_trust_doctor.py" --cwd "C:\path\to\workspace"
```

Linux、WSL 或 macOS：

```bash
codex_home="${CODEX_HOME:-$HOME/.codex}"
python3 -B "$codex_home/plugins/cache/workflow-manager/workflow-manager/1.0.66/scripts/hook_trust_doctor.py" --cwd /path/to/workspace
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
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.66 --json
```

如需回退，先移除插件和市场，再使用目标标签重新添加：

```bash
codex plugin remove workflow-manager@workflow-manager --json
codex plugin marketplace remove workflow-manager --json
codex plugin marketplace add LOLer001/workflow-manager --ref v1.0.13 --json
codex plugin add workflow-manager@workflow-manager --json
```

## 工作方式与边界

- **普通任务**：Codex 原生完成规划、工具调用、并行、恢复和压缩；插件不启动评估者，也不增加流程格式。
- **Hard 任务**：确认前只允许读取、搜索和静态检查；确认后由当前 slice 的唯一写入者按合同实施。父会话可在无子写入者且诊断已完成时直接取得租约，或选择一个绑定子执行器；两者不得重叠。
- **恢复与变更**：同一授权范围内的修复和恢复可连续推进；目标、风险或不可逆操作发生实质变化时重新确认，身份或证据不一致时安全拒绝。
- **插件边界**：只负责 Hard 授权、宿主运行证据、单写者和外部安全；不接管 Codex 的日常计划、调度、模型判断或输出格式。

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
