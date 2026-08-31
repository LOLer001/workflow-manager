# 更新记录

## 1.0.57

- 将 Hard 合同从“必须一个子执行器”收敛为“同一时刻恰好一个写入者”：严格确认且当前无 pending/live/unknown 子写入者、无未完成 causal/stall 诊断时，父任务原子取得当前 contract/slice 的唯一租约，可连续实现、失败修正、验证与发布；租约存活时拒绝 child spawn，子写入者存在时父写入 fail-closed。
- 父级接管 `verification_required` 旧候选会单调增加 attempt、清空旧 review candidate，并将后续宿主操作绑定当前 epoch/contract/slice；父级成功变更与验证可由 PostTool/Stop 证据封存，无需伪造 child Stop。挂载树 Git、设备与新增不可逆动作边界不放宽。
- 错误 profile 的 assessor Start 保持 `recovery_required/start_mismatch` 且结果永不具权威；仅当前 epoch、request、contract、attempt、agent 与 rejected generation 全部唯一一致的真实 `SubagentStop` 可将其 liveness 幂等终结为 `terminal`，迟到、字段冲突或复用 id 事件只写隔离 tombstone，不影响 attempt 2。
- 释放已由完整宿主 inventory 明确缺席的 request-only 高阶 assessor 预约，并隔离其迟到 Start，避免绑定后继预约。
- 测试临时目录仅在 POSIX 的真实 `/tmp` 下固定；Windows 改用系统临时目录，标准 unittest 入口不依赖当前工作目录。
- 升级 Schema 33 / writer 1.0.57 / execution profile v12。历史 sealed success 保留原 profile；未完成 v11 写权不会静默升级，证据不唯一时隔离并 fail-closed。
- 强化 TaskEpoch、append-only v3 root 选择与 writer liveness：迟到事件只能唯一绑定原 epoch，缺少唯一完整 inventory 的 child 进入 `isolated_incomplete`，未知终态不伪造成功。
- continuation outbox 采用 `epoch + contract + reason_digest` 稳定键；stdout 仅为发送尝试，只有精确宿主 receipt 或 root-visible 匹配消息可确认消费。
- `git tag` 门禁按三态解析：list/verify 查询保持只读，创建、删除、重指向、混合、动态或不完整 argv 均作为 mutation fail-closed。

## 1.0.56

- 修正 1.0.55 引入 TaskEpoch 后的 C24 首次写入竞态夹具：外部竞态文件现在写入当前 state 的 epoch-scoped canonical journal，而不是 legacy session journal；产品的 no-clobber、事务回滚与 fail-closed 语义不变。
- 采用 forward-only 发布，保留既有 1.0.55 Tag/Release 与历史；Schema 维持 32、execution profile 维持 v11，并保留 Schema 32/writer 1.0.55 活跃合同的懒迁移连续性。

## 1.0.55

- 引入隔离 TaskEpoch：真实的新目标或跨工作区任务拥有独立授权包络与 canonical journal；仅 worktree 迁移不会清除既有合同。
- 旧 epoch 按审计状态封存；证据不完整标记为 `isolated_incomplete` 或 `authority_unknown`，活动 writer 会阻止切换而不会被静默重定向。
- continuation lease 绑定 epoch，避免旧任务的迟到或重放 Stop 消耗新任务的续接。
- Schema 升至 32；仅迁移 1.0.54 历史状态，不改写历史 journal 或制造成功。

## 1.0.54

- 恢复证据绑定为不可变 root session、root cwd 与首个宿主 regular rollout 文件的 device/inode 身份；跨 cwd、替换文件、符号链接或不唯一的 rollout 一律不用于恢复 parent 控制面。
- 历史项目内 `.codex/plans` review mirror 仅作为污染证据，活跃合同 fail-closed：不再读取、解析、迁移、写入、清理或向执行器交付其正文。新增有界结构化 `legacy_plan_rejected`、`root_identity_mismatch` 生命周期诊断。
- Schema 升至 31，writer/plugin 版本升至 1.0.54。

## 1.0.53

- 修正 1.0.52 安装后 trust doctor 的发布元数据：`DISPATCH_STATE_SCHEMA` 与实际 Schema 30 收据一致，不再把已由新任务证明加载的 1.0.52/30 运行时误报为 `runtime_mismatch`。新增 doctor writer/schema 与 Hook/manifest 版本矩阵的防复发断言；运行时 mailbox/recovery 语义不变，并保留 Schema 30/writer 1.0.52 活跃 profile-v11 执行的懒迁移连续性。

## 1.0.52

- 修复宿主已在 `list_agents` mailbox 中返回唯一绑定执行器的 `completed`/`FINAL_ANSWER`，但漏发 `SubagentStop` 时执行合同永久停留在 `running` 的生命周期缺口。Schema 30 仅在 request、成功 Post、full Start、agent/task、contract、slice 与 attempt 唯一一致，且 completed 正文最后一行是严格匹配的 `EXECUTION_RESULT` 时追加独立 `mailbox_terminal` 等价边界；不伪造或增加 `SubagentStop`，`wait_agent` 摘要、running/commentary、普通未绑定 agent、歧义、乱序重复和合同漂移均拒绝。
- exact typed recovery 若早于该终态到达，只保存合同绑定的 digest-only `terminal_pending` 记录且不授予 spawn/mutation 权；匹配终态形成后自动提升，终态先到时后续 recovery 也可使用 mailbox 报告的 failure/evidence 摘要。保留 Schema 29/profile v11 活跃执行的懒迁移连续性，压缩与重复投递保持幂等，原始 prompt、回答、根因和修正文本均不持久化。

## 1.0.51

- 将 Linux GitHub Actions 全量测试 job 的上限从 10 分钟提高到 20 分钟，覆盖 316 项套件在托管 runner 上的正常耗时波动；测试矩阵、断言、Windows 30 分钟上限、Schema 29、execution profile v11 与运行时行为均不降低或改变。

## 1.0.50

- 修正 Windows 九事件发布夹具：普通未绑定 `SubagentStart` 自 1.0.49 起会持久化一条 `ordinary_spawn_no_active_hard` 信息诊断，因此授权/连续性/诊断事件总数应为 7；运行时行为、Schema 29、execution profile v11 与 journal v3 合同不变。

## 1.0.49

- 修复 Desktop 短确认：原始消息先限长，随后仅裁剪外围空白；接受外围 LF/CRLF，拒绝内部换行、代码块和附加条款。支持“计划生成 → 查看计划 → 短确认”与安全早到确认续接。
- 升级到 Schema 29 / execution profile v11：canonical journal v3 只追加 typed executable revision、terminal seal 或 durable conclusion，保留既有 v2 字节前缀；尾部结论不授予执行权，也不重写已完成 revision。
- 将同会话完成后问题分类为 direct follow-up、回归、副作用、验收缺口、执行暴露、只读不确定调查或无关新目标；没有 change-set 时禁止归因为 introduced regression。
- 修复“不可逆外部动作无”的无冒号否定识别，并避免普通“版本”问答改变 reference 合同。

## 1.0.48

- Windows CI 固定使用 `actions/setup-python` 提供的 Python 3.12 并启用 UTF-8，不再由 `py -3` 绕开已声明解释器、在中文隐私夹具上触发 cp1252 写入失败和逐项 10 秒等待；Windows 文件安全夹具先 canonicalize 临时根并以二进制 LF 写入，产品的 canonical-path 与 canonical-byte 拒绝边界保持不变。
- execution-slice manifest 改为可选增强：parent 的普通原生计划无需 JSON/fence/固定结尾；缺省时完整计划投影为一个逻辑 slice，显式 manifest 则使用 196608-byte / 1024-node 总预算且无独立 slice/list 上限。正常计划可由模型选择 3–5 slice，长计划按预算扩展，预算不足只能停止或拆分而不能降低验收。
- 补丁 reconciliation 同时支持 legacy 唯一成功 `patch_apply_end` 与当前宿主精确空 `{}` 成功回执；同 turn 多次补丁只按唯一 call id、唯一字面 digest、唯一回执和宿主 operation 顺序一一绑定，重复、额外 sibling、拒绝或歧义仍保持 unknown，不从 `FileChange` 推断成功。Desktop 仅遗漏通用 status 时，精确绑定 executor/contract/slice/input/command/turn 的 PostToolUse 改动可由后续独立 parent verification 封存，不再强制 recovery child。缓存清理明确保留当前版本及紧邻的 1.0.47 回滚缓存。
- Workflow Manager 插件/Hook/Skill 的版本化生产发布作为窄 Hard 分类；普通介绍与聊天不受影响。
- State Schema 升至 28、writer/manifest 升至 1.0.48、stable-skill schema 升至 9，execution profile 保持 v10。
- 提高 Hard 判定阈值，高置信 Simple 完全使用当前 Codex 且 Workflow Manager `Start=0`；普通规划、并行、进度、常规恢复和 compaction 交回宿主，Skill 只保留 Hard 授权、唯一 writer、挂载树安全与不可替代的 host lifecycle/acceptance 证据。
- 固定门禁进一步收敛为宿主 lifecycle 真值、授权 envelope、单 writer/禁 nesting、挂载树 Git 与破坏性外部动作安全；计划 prose、列表/切片/阶段数量、普通输出形态、墙钟和原生调度不再形成独立门禁。显式 `irreversible_action:none` 只表示无不可逆动作，不再仅因字段名触发 Hard。
- Hard assessor 改为进度与预算驱动的活性：600 秒仅观察、恰好 1200 秒仍 live，严格超过 1200 秒无进展时主动诊断、解卡或拆分，不直接变成 `assessment_timeout`、`blocked` 或 exhausted。轮询、迟到事件、压缩恢复和时钟回拨不重置进度。
- typed recovery 统一核对原 assessor 的唯一 PreTool request、PostTool `host_accepted=true`、唯一 full Start 及扁平状态，严格绑定 binding/objective/sequence/model/effort/fork；缺少、未知或拒绝 host acceptance 为 `model_unavailable`，其余 lifecycle/state 冲突为 `start_mismatch`，不再从扁平字段或 child 消息推断。
- 适配当前宿主成功 spawn 只返回 canonical `task_name`、不带通用 status 的真实回执：仅同 request fingerprint 且精确 `<task_name>`/`/root/<task_name>` 的首个结构化 Post 可形成 acceptance，重复同回执幂等，未知、拒绝、错名或冲突回执不可升级。Start 先于 Post 时只投递 digest-bound locked handoff，写入仍拒绝；精确 Post 到达后在同一状态锁内复验 journal 才解锁。
- 默认正常 confirmed executor 保持 lower-tier + `medium`；failure/stall/incomplete/verification recovery 使用当前最高可用 `gpt-5.6-sol` + `max` + `fork_turns=1`，显式 `highest_throughout` 才使用最高档 + `ultra`。SubagentStart 同时接受并验证这一 typed-recovery profile。
- executor sequence 改为受状态字节/节点预算保护的单调序列；`task_name` 仅是任意安全 ASCII 宿主标签，不再编码 contract、slice、sequence、failure fingerprint 或 review digest。reservation 前原子核对 failure/evidence/progress/root cause/material correction：同失败指纹且无新证据的原样重放被拒绝并要求诊断/换方案，有新证据、根因、实质修正或不同 fingerprint 可继续，三次以上不同 fingerprint 不会按固定次数耗尽。terminal child 不复活、不嵌套，始终只有一个 live writer。
- 私有 Start handoff 由可信状态补齐宿主签发的 `execution_contract_id`、objective、plan digest/generation 与当前逻辑 slice，子执行器无需也不得扫描插件状态；request prose 和 task name 均不承载授权。Desktop 的 `opaque_v2` recovery 由 Hook 从 request/Post/full Start、终态、operation ledger 与 parent review 生成 digest-only failure/evidence 事实，parent 在当前 confirmed envelope 内补充 root cause/material correction后原子预约；合法恢复沿用既有确认且不持久化错误/诊断 prose。
- authorization envelope 只绑定 objective 与显式 acceptance/risk category/irreversible external action 的归一化摘要，不绑定计划 prose、slice 布局或 manifest digest；同范围 repair/autosplit/verification/compaction successor 自动继承严格确认，只有 envelope 字段实质变化才重新确认。
- assessor 已完成但 parent Stop 尚未提交可信 revision 时，提前纯确认只保存 host-bound receipt digest，同时保留 pending plan、Hard binding 和 repair；可信 revision 落盘后自动绑定，不重置 Daily 或要求用户重发。Schema 27 活跃 assessor 按当前 binding 重锚；canonical journal v2、accepted slice 与 profile-v10 宿主证据保持原样。
- 三层绑定成立后，assessor 的普通非空只读分析只生成 digest-only 终态收据，parent 以非空且在总预算内的普通计划写入唯一 canonical revision；不再要求 `WORK_ASSESSMENT`、manifest fence、固定关键词、固定尾句、最小 prose 字节数或任意列表格式。已收到的纯确认可由同 session host rollout、唯一 lifecycle 与 parent `task_complete` 自动续接，可信 revision 落盘后绑定，原文不持久化。
- executor 与 parent review 均接受普通非空原生 prose；`EXECUTION_RESULT`/`EXECUTION_REVIEW` 仅作可选兼容标记，显式畸形或冲突才拒绝。真正的强门禁是当前合同的宿主 operation ledger、独立 parent verification 与单 writer 证据，缺少真实 verification 才进入 `incomplete_execution`。
- 核心 lifecycle 锁改为宿主事件串行等待，避免固定 0.75 秒争用时静默丢弃 Stop/确认；Hook 进程 45 秒仅是进程安全上限，不是工作流或 assessment deadline。
- 当前 Codex 直接运行项目内 custom verifier 时，不再因命令不叫 `pytest/unittest` 而丢失 verification：只读且实际执行的 `verify/validate/check/acceptance/regression` 程序及其唯一结构化宿主结果可重分类；源码查看、`python -c` 字样和带写入/重定向的命令不能冒充验收。normalize-only 的可信宿主恢复会在只读 snapshot 原子持久化，不再等下一次无关 mutation。
- 新增 1.0.43 匿名冻结 trace 正式 A/B：child Start 8→3（降低 62.5%，门槛 ≥60%），重复 `additionalContext` 856→153 UTF-8 字节（降低 82.1%，门槛 ≥50%），Hard executor 强验收检查点不减少；该静态指标明确不冒充真实 token。

## 1.0.47

- 保持 Schema 27、execution profile v10 与 stable-skill schema 8；新增私有、有界、无原始输入/输出的 Hook dispatch receipt，并让 trust doctor 分别报告配置与指定会话 dispatch 状态。
- 发布工作流允许回填已有 CHANGELOG 且标签内 manifest 精确匹配的缺失版本，不再为 v1.0.46 设置特殊拒绝；回填旧版本不会取代较新版本的 GitHub Latest 标记，Linux/Windows runner 均保持 Hook 入口后、业务 state 前的 receipt 语义。
- 修复实机暴露的“发现问题只 fail-closed”循环：带说明的重规划句和“作废/修正版/写入”可直接触发 replan，活动 Hard 的 delegated recovery 不再误降为 Daily 并清空 binding。
- 删除两项只增加额度、不增加信任的 assessor 格式门：绑定 Start/Stop 已证明身份且计划结构有效时，Hook 可生成 assessment digest；唯一尾部普通 `json` execution-slice manifest 会被严格校验并规范成 canonical fence，不再为补 marker/fence 重启最高模型。
- confirmed executor 必须让长测从进程启动即进入前台 deadline，保留 outer/inner 终态并在 Stop 前收口；自身引入且可回滚的命令合同错误须在当前 live ownership 内立即修复重跑，不得升级成 replacement-plan 循环。
- 修复稳定 Skill 更新只覆盖不删除的问题：安装器现在记录逐文件摘要，并只删除与历史发布字节完全一致的退休受管引用；用户新增、修改、链接或未知文件保留。由此清除会让新任务继续读取旧 agent lifecycle/实时协调规则的残留文件。
- 修复真实父审已完成却因 Desktop Stop 漏字段被覆盖为 `verification_failed` 的循环：resume 只在同 session rollout 的唯一精确 review、匹配 `task_complete`、全部 bound structured verification 成功且无后续 mutation 时直接自愈封存；多条验收命令不再被“必须恰好一条 exec”的旧桥拒绝。已封存前序切片的 child Stop 状态遗漏也通过唯一 full-Start owner、精确 succeeded result、父审和无冲突写操作保守补齐，不启动第三个 executor。
- 修复自愈后的状态查询因 repair 标识符包含 `_change_` 而被误判为“修改计划”、清空已封存合同并请求新 assessor 的问题：英文计划变更词改为单词边界匹配，完成态状态/证据查询保持原 `succeeded/passed`，真实范围变更仍会使旧授权失效。

## 1.0.46

- Schema 升至 27、writer/manifest 升至 1.0.46、execution profile 升至 v10、stable-skill schema 升至 8；Schema 26 的 canonical Hard 计划、宿主生成证据和封存父审继续按严格迁移边界保留。
- 按 Codex 当前官方能力重新划分职责：普通规划、进度、工具使用、错误恢复、压缩、模型选择和子智能体编排全部交回宿主；Workflow Manager 只保留 Hard 授权、canonical 合同、Start 运行时真值、父审证据与挂载树安全。
- 删除通用 Direct/Focused/Complex/Extensive 路由提示、阶段顺序、agent cap、side-lane/child 嵌套规则、70% 压力提示、普通失败自动升档、通用 Start 结果格式和跨任务实时协调协议；旧状态中的对应字段迁移时丢弃，不再重新注入。
- Daily 与全部非 Hard Work 直接使用原生 Codex，Workflow Manager 不再启动 Simple assessor；只有达到既有高门槛的 Hard 新目标才创建一个 `max` 评估者，确认后仍使用唯一当前切片 executor 与完整 Start 观测。
- 删除 assessor 的旧 Simple 判定、二阶段 follow-up、`SIMPLE_EXECUTION` 结果和父任务 Hard 计划绕行；Hard assessor 现在只有一个只读计划协议，普通 child 的请求、Start、Stop 完全透传宿主且不进入插件账本。
- 修复真实宿主已记录 `Start=full` 但父任务仍声称“未暴露运行时回显”的信息断层：bound SubagentStop 记录 requested、host accepted、Start 状态、观测 model/effort 与来源，并在父任务首次 wait/list 收割边界精确回传；assessor、executor、去重和非 Hard 静默均有回归锁定。
- 适配 Codex 0.149 的新版宿主 rollout：不再要求整个 turn 只有一个 `exec`，改为按唯一 call id 与 command digest 逐链核对；支持 JSON 参数 `tools.exec_command({...})`、`item_completed/FileChange`，并从多次尝试中只回收当前 attempt/candidate。重复 id、重复 digest 或不唯一 FileChange 仍 fail-closed。
- 明确父审时序：`EXECUTION_REVIEW` 只能作为结束 turn 的唯一末行，Stop 封存后再 resume 当前下一切片的最小 delta，避免同 turn 提前 spawn 与重复 marker。
- confirmed Hard 的 SessionStart 不再重复注入完整 canonical 计划：Hook 内部仍校验完整 journal，只投影当前 slice、global constraints、contract/attempt/review 与 completed-prefix digest；定向回归限制该恢复上下文低于 6 KB。
- SessionStart 只在存在活动 Hard/因果/参考合同时回放有界合同元数据；普通新任务与普通压缩恢复只得到一条短身份边界，不再重复完整工作流账本。
- 新增“无 Workflow Manager / 1.0.45 / 精简 1.0.46”同题困难任务三臂模拟与宿主原生透传回归；正确性证据和强验收必须相同。固定样例的插件 `additionalContext` 从 1489 UTF-8 字节降至 543（约 63.5%），该数值不冒充真实 token。

## 1.0.45

- Schema 升至 26、writer/manifest 升至 1.0.45、execution profile 升至 v9、stable-skill schema 升至 7、difficulty classifier 升至 v2；旧 1.0.44 状态按来源 Schema 保留严格 transcript/host evidence 迁移边界。
- 提高 Hard 门槛：只有单个关键风险，或至少两个独立强信号组且包含诊断、范围、连续性之一才进入困难计划；设备、构建、三阶段、共享资源或含糊文字不再单独触发 assessor。
- 将硬门禁收敛为授权相关边界：未确认 Hard 写入、错执行者/错切片、挂载树 Git 与破坏性/外发操作。大输出、重复只读、阶段计数、55% 压力提示和常规命令形态只保留遥测，不再打断解决过程。
- 首个普通失败要求原路线立即做一次实质修正；未知根因、关键风险或修正仍失败时，主动请求最高可用模型与 `max` 做一次绑定诊断，然后继续最小修正，只有真实外部阻塞才停止。
- 路由上下文改为事件驱动，只在目标、路由、授权或恢复边界变化时注入；移除重复成功、阶段预算、55% 压力和大输出 `additionalContext`。稳定 Skill 缩为薄的连续性与授权层，不复述新模型原生的编码、进度、工具及通用多智能体规则。
- 困难计划切片上限降至 6，正常为 1–3；并行容量按当前 active/reserved 通道计算，终态后可复用，允许明确只读且不重叠的 child lane，仍拒绝 child-origin mutation。
- 增加 AndroidNativeDemo 历史任务的匿名额度审计与同题三臂 A/B 模拟；直接 Workflow Manager 上下文开销必须相对 1.0.44 基线显著下降，且正确性证据和强验收不降低。真实 token 无法从累计计数中可靠归因时明确报告边界。

## 1.0.44

- Schema 升至 25、writer/manifest 升至 1.0.44、execution profile 升至 v8、stable-skill schema 升至 6；迁移保留已封存 host evidence 与父审候选，但 v7 的 pending/running 状态绝不获得 v8 写入权。
- Hard assessor 默认请求最高可用模型与 `max`；只有显式 `highest_throughout` 请求 `ultra`。所有绑定 child 固定 `fork_turns=1`，confirmed executor 保持 lower-tier + `medium`。
- 执行切片硬上限降为 8（正常 3–5）；恢复仅携带 global constraints、当前切片和游标后的最小 delta。重复宽搜/精确只读/视觉探针按变更 epoch 节流，变更后强验收重新放行。
- Simple 不启动额外 assessor；child-origin spawn fail-closed；side-lane 总启动预算默认 1，只有显式 ready+disjoint 的独立并行最多 2，预算单调耗尽且不得为降级验收补充。
- 将 child profile evidence 明确拆为 `requested`、`host_accepted` 和 Start `full|partial|absent|mismatch`：Start model 仅来自官方 Hook payload，effort 仅来自同 turn 宿主 transcript 的 `turn_context.payload.effort`；旧 event_msg 兼容来源单独标记，缺失或冲突一律不授予 bound mutation。
- 修正 projectless 文件产物/工程验收合同被低置信默认路由误判为 Daily：显式 Work/Hard 工程合同、创建并验证文件产物，以及跨真实 host compaction + same-session resume 的连续性任务分别进入 Work/Hard；`do not edit files` / `不要创建或修改文件` 等否定边界仍保持 Daily，并以正反 gold-set 回归锁定。
- 修正身份/激活预检中的 Work/Hard 文字误触发 assessor：只有同时显式禁用 tool 与 child 的预检才进入 Daily/direct 零 Start 路由；替换中断预检时审计并清除陈旧 running assessor，后续 spawn 仍由 Hook fail-closed 拒绝。
- 修正 cachebuster/reload 后 `identity_evidence.plugin_root_fingerprint` 沿用旧会话值：每个成功持久化的当前 Hook 事件都从实际 `PLUGIN_ROOT` 刷新身份回显。

## 1.0.43

- 困难任务计划生成默认改为“最高可用模型 + 第三高推理档 `xhigh`”；推理档位按 `ultra > max > xhigh > high > medium > low` 排序。只有显式 `highest_throughout` 会话偏好继续请求 `ultra`，确认后的默认低档执行切片仍固定 `medium`。
- Schema 升至 24、writer/manifest 升至 1.0.43、execution profile 升至 v7、stable-skill schema 升至 5，并新增发布版本矩阵校验，拒绝任一版本或协议指标漂移。
- v7 executor/review marker 改为严格唯一、无缩进、最后非空整行：`EXECUTION_RESULT execution_contract_id=<32hex> slice_id=sNN outcome=succeeded|failed` 与 `EXECUTION_REVIEW execution_contract_id=<32hex> slice_id=sNN outcome=passed|failed`。模型不再手写 evidence digest；Hook 从合同、切片、尝试、完整结果和调用者绑定生成/规范化有界证据，重复、缩进、嵌入、错合同或错切片均 fail-closed。
- 每个 Hard canonical 修订末尾新增唯一 `workflow-manager-execution-slices` JSON manifest：1 到 32 个连续 `sNN` 切片分别绑定全局约束、范围、验收、回退、停止条件和预期产物。Hook 以全局合同、manifest、当前切片和已验收前缀派生可见 token；至多一个切片写执行者存活，父审通过后才推进下一片，只有最后一片通过才 sealed 全局成功。
- 每片初次失败后最多一个合同/切片/token 绑定 fresh v2；父审失败的 opaque 名称携带 Hook 生成的 review evidence，不复活 terminal v1，也不允许低档执行者跳片、提前封存或通过摘要格式错误绕过强门禁。
- Schema 23 已有 passed baseline 的 sealed v6 成功保留原 profile/contract；v6 review candidate 仅延续只读复核，旧自报 digest 只在该边界兼容且其值被忽略后由宿主规范化。其他无合法 manifest 的活动 v6 状态没有 v7 写权，须向同一 canonical Markdown 追加完整修订、重新严格确认并生成新合同，失败复核不重置恢复预算。
- 安装器、Unix/Windows Hook launcher 与仓库校验统一禁用 Python bytecode；稳定路径覆盖已验证后只清理严格更旧且可证明自有的缓存/残留，保留更高版本、非版本目录、符号链接和不能安全判定的条目。

## 1.0.42

- Schema 升至 23、writer/manifest 升至 1.0.42、execution profile 升至 v6、stable-skill schema 升至 4。已有 `acceptance_status=passed` 的 sealed 历史成功继续保留真实 profile/contract；活动或普通失败的 v5 合同重绑 v6。
- confirmed executor 的精确 `EXECUTION_RESULT ... outcome=succeeded` 现在只进入 `verification_required` 候选态并保存有界 `executor_review`；父会话独立只读验收后，只有精确 `EXECUTION_REVIEW ... outcome=passed` 才原子封存 `succeeded` 与 passed baseline，错合同、缺失、重复或畸形 review 均不能封存。
- 父级验收失败在 attempt 1 可直接创建唯一 fresh v2；其可见 task name 同时绑定旧 contract、`verification_failed` 和 32 位验收证据摘要，plaintext 还须匹配 `recovery_from`、`material_correction` 与 `verification_evidence_digest`，opaque V2 依赖精确名称、review binding、terminal 边界及正数 fork。v2 候选仍需父级复核，失败则 exhausted。
- Schema 22 的 `succeeded + baseline incomplete`（包括后续错误拒绝残留的 `invalid_spawn_config`）迁移为原 v5/旧 contract/attempt 1 的 review candidate，清除虚假 failure 且不依赖会被升级清理的 transient subagent cache；被拒 executor spawn 不再污染候选或已 sealed 状态。

## 1.0.41

- Schema 升至 22、writer/manifest 升至 1.0.41、execution profile 升至 v5、stable-skill schema 升至 3；活动/失败的旧 v4 合同重绑 v5，已有 sealed baseline 的历史成功继续保留真实 v4 合同与 profile。
- matching confirmed executor 的 `SubagentStart` 现在先从受信 plugin-data canonical journal 验证并读取 current revision，成功后才进入 `running`，并把 exact body 私下交给 child；相对路径明确只属于 plugin-data-root contract metadata，`cwd` 同名文件、读取失败和摘要漂移均不能解锁写入。
- 初次 executor 使用可见 v1 task name；普通失败后的唯一恢复必须以绑定 current contract、failure kind 与 attempt 2 的可见 v2 task name 新建 child。terminal v1 的 `followup_task` 明确拒绝，opaque V2 recovery 仅凭精确 v2 名称、当前失败/尝试及正数 fork fail-closed 接受，第二次失败 exhausted。
- `mkdir` 纳入文件变更识别，未确认计划和未绑定 recovery executor 不能预建空目录；补充真实 lifecycle/journal 模拟回归覆盖 private handoff、cwd decoy、drift/read failure、fresh v2 写权限和迁移。

## 1.0.40

- Schema 升至 21、writer/manifest 升至 1.0.40、execution profile 升至 v4、stable-skill schema 升至 2。
- 新 objective/replan 原子失效旧 assessor/plan/executor 绑定；Daily 切换同步清除旧 failure、binding 与 observed profile，旧失败不再冻结新任务。
- 真实 `create_thread` 的严格 `<codex_delegation>` 首条包装现在提取并路由其中的 bounded input；畸形、混合或伪造结构仍按协调控制 fail-closed，不再吞掉新任务 objective。
- writer 迁移将活动/失败合同重绑到当前 execution profile，并清除不再有授权效力的旧 lifecycle/coordination 缓存；已有 sealed baseline 的成功历史合同保留其真实 profile/contract，绝不伪造 v4 成功或重新执行。
- stable Skill 验证成功后仅在 canonical 插件缓存边界内清理旧插件版本；保留 migration marker/lock 作为并发安全哨兵，运行器仅保留当前 SHA-256 内容寻址缓存。

## 1.0.39

- 在 v9fs 等不支持 `renameat2(RENAME_NOREPLACE)` 的挂载上，当其返回 `EINVAL`、`ENOSYS`、`ENOTSUP` 或 `EOPNOTSUPP` 时，canonical transaction 改用保持 no-clobber 语义的 hard-link fallback；链接后验证常规文件身份、设备/inode 与链接计数，随后才移除私有源文件，竞态或身份异常均 fail-closed。
- 新增 S14 回归，覆盖 `renameat2` 不支持时的成功发布、源文件清理、目标链接计数恢复，以及既有目标绝不被覆盖。
- Schema 保持 20，writer/manifest 升至 1.0.39。
- executor profile 升至 v3：缺失终态 status 时仅接受唯一、严格匹配当前 execution contract 的 `EXECUTION_RESULT ... outcome=succeeded`；`failed`、错合同、重复、畸形或空 marker 均 fail-closed，显式失败/取消也永不成功。

## 1.0.38

- 兼容 Codex Desktop 真实 `SubagentStop` 事件缺少 `status` 的载荷：精确匹配 request/Start 且结果非空时，允许绑定 assessor/executor 继续各自既有 marker、计划、目标或执行合同校验；普通 lane 的持久化状态仍为 `terminal/unknown`，缺失状态本身不成为成功证明。
- 显式 `failed`/`cancelled`、空结果、坏 assessment marker、陈旧目标、旧执行合同、重复或晚到 Stop 继续 fail-closed；补充真实 Hard assessor、confirmed executor、无效 marker、保守 unknown 与终态幂等回归。
- Schema 保持 20，writer/manifest 升至 1.0.38；canonical Markdown、960 KiB/10 MiB 容量、漂移失效和 `marker → journal → state → cleanup` 合同不变。

## 1.0.37

- Hard 详细计划只有在成功写入固定私有 `plans/<session-token>/hard-plan.md` 后才进入 `awaiting_confirmation`。同一会话的每次重规划或新目标都把完整修订追加到同一 canonical Markdown；当前受信修订定义计划内容，但文件本身绝不确认计划或授权执行。
- 查看详情、重规划、压缩恢复和唯一执行者必须重读 canonical 当前修订；`update_plan` 只能是携带当前修订摘要且步骤文本一致的 UI 投影。外部编辑、替换或摘要漂移会立即进入 `invalidated`/`stale_contract`，旧确认与执行合同不能复用。
- 单次修订恰好 `983040` 字节、全文恰好 `10485760` 字节允许写入；再多 1 字节分别以 `revision_too_large`、`journal_full` 拒绝，日志逐字节不变且代次不增加。跨文件提交按 `marker → journal → state → cleanup` 执行，崩溃恢复只接受 old/old 或 new/new，其他组合 fail-closed。
- Schema 升至 20，writer/manifest 升至 1.0.37。Schema 19 最多迁移 6 个严格可验证的 v1 镜像，缺失、漂移、不可解析、超量或活动执行合同全部安全失效；旧镜像只在 canonical 日志与状态共同提交后清理。

## 1.0.36

- 兼容 Codex Multi-Agent V2 在本地 `PreToolUse` 前将 collaboration `message` 加密的真实载荷：assessor 与 confirmed executor 改用可见、目标/合同绑定的 ASCII `task_name` 加正数 `fork_turns` 作为预派发状态绑定，明文宿主继续执行原有完整 message 合同校验。
- V2 Simple 二阶段只允许 follow-up 的可见 canonical target 命中此前已接受的 bound assessor task；错 target、旧 binding、旧目标、无正数 fork 和旧合同全部 fail-closed。结果端仍要求 `WORK_ASSESSMENT` / `SIMPLE_EXECUTION` binding，Hard assessor 提前变更仍失败，executor 仍受单一所有权与验收门禁。
- 加密 follow-up 无法暴露 `stall_id` 与 `execution_contract_id` 时，stall recovery 明确拒绝并要求重规划，不把宿主可见性缺口变成授权降级；同理 opaque assessor/executor recovery 不复用旧合同。
- Git 挂载守卫适配官方 `Bash` Hook 只保证 session `cwd` 的形态：可从命令本身安全解析一个或多个字面量 `git -C`，仍拒绝 DrvFS/CIFS/UNC、虚假 `/tmp`、无法安全解析的相对/动态路径和单次命令中的多个 Git 调用，避免“安全首条 + 挂载区第二条”的链式绕过。
- Schema 升至 19，writer/manifest 升至 1.0.36；只新增 `plaintext|opaque_v2` 枚举，不保存 collaboration 明文或密文。Schema18 迁移不会凭旧字段生成 V2 授权证据。

## 1.0.35

- Hard 详细计划进入待确认状态时，自动在插件私有数据目录生成经过清理和大小限制的 Markdown 审阅镜像，并将相对路径、目标/难度/代次、`plan_digest` 与正文摘要作为有界状态保存。镜像只用于审阅，不能确认计划或授权执行；状态中的 `plan_digest` 始终权威，正文修改只会标记 `content_drift`。
- 镜像写入采用真实私有目录、无链接常规文件和原子替换；写入失败、漂移、压缩恢复或路径身份异常不会自行锁定确认，同一计划仅在真实原因修正后安全重试。Schema 升至 18，writer 升至 1.0.35，旧 Schema 17 只迁移绑定元数据，不臆造计划正文。
- 每个会话确定性保留当前镜像和最新 5 个受管旧镜像；单个清理事务最多 16 项，每项删除前先绑定 identity、受限字节快照与 mode。目录替换、符号/硬链接、同名竞态或删除前后校验失败会逆序恢复整个事务，已删除和未删除对象均须逐字节恢复且不覆盖突然出现的路径。
- 修正 Windows 目录 guard 的读取权限请求，保持共享模式不变，避免请求不必要的目录写权限；新增 Linux/Windows 原生计划镜像、目录交换、外部链接隔离、全事务回滚和 16 项边界回归。

## 1.0.34

- 子智能体记录改为按完整生命周期折叠，不再机械保留最后 24 条事件：pending 请求、live Start、无第二次 Start 的同代理 follow-up，以及 terminal Stop 作为代次整体参与计数、门禁、压缩和恢复。
- Stop 缺少 status 时仍以 `terminal/unknown` 幂等关闭；重复、孤立、缺 ID、晚到 Start/Stop 不会复活或误停代理。同一 ID 新代次必须有更新请求，复用后 Stop 还需匹配当前 request fingerprint/turn，绑定 Simple/stall marker 保留无第二次 Start 的安全收敛。
- pending 请求与 Start 在同一状态锁内单次消费；并发 Start 或两个 confirmed executor 只能有一个生效，输家明确记录生命周期拒绝，不能降级成无绑定普通 lane 或静默驱逐既有执行证据。
- 永久保留 pending、result-pending、live 及当前绑定 assessor/executor；完整终态历史只保留最新 10 组。保护集达到安全上限时拒绝新委派，不通过丢弃活动证据腾空间。Schema 保持 17，writer 升至 1.0.34，仍只保存有界指纹、枚举、长度和时间。
- 主 Skill 明确宿主边界：结果返回后仅当宿主仍显示该精确代理 running 才停止；不碰 live/待回收/诊断/合同执行代理。Hook 不能调用宿主状态 API、终止代理、删除任务或清理侧边栏历史，也不会虚报这些动作。
- 新增终态幂等、ID 复用、延迟 Stop、并发 pending、双 executor、25 pending、完整终态裁剪、compact/resume 和隐私回归，并复跑实时协调、卡顿诊断与严格困难计划合同。

## 1.0.33

- 已确认困难计划的绑定执行者只有在当前类型化失败后停止变更并提交精确 `EXECUTION_STALL` 行时，才进入卡顿升级；普通首个实现、编译、部署或验证失败继续使用既有原档修正，不会把日常错误机械升级为高成本流程。
- 卡顿诊断复用原 objective-bound 最高档评估者并保持首轮严格只读，不创建第二个诊断代理；请求精确绑定 stall、assessor、目标和执行合同，诊断完成前拦截执行恢复、旧执行者/父会话变更及无关代理创建。
- 同一锁事务只允许一个诊断 follow-up 获得 pending ownership；明确投递失败最多正常重试一次，未知回执按可能已送达处理并禁止自动重投，避免双投递。坏 marker、错调用者/绑定、晚到结果、第二次失败或同合同再次 stall 均 fail-closed/exhausted。
- `resume` 只接受不扩大已确认计划的有界修正，并要求恢复请求携带 stall/remediation 摘要、类型化失败和实质修正；默认恢复 lower-tier+medium，显式 `highest_throughout` 则恢复原最高档。成功后 resolved，恢复失败不再二次升档；`replan` 会使旧计划和执行合同失效并重新严格确认。
- 状态 Schema 升至 17；压缩、恢复与 Schema16 迁移只保存卡顿状态、指纹、枚举、尝试、原执行档和时间，不保存错误、提示、命令、计划或子智能体结果原文。新增并发 one-shot、未知回执、resolved 普通 follow-up、默认/最高档恢复、replan、失败耗尽与隐私回归。

## 1.0.32

- 跨任务共享资源协调改为即时证据门禁：只有 fresh `list_threads` 同时证明当前任务和目标任务处于同一宿主、不同任务且均为 `active`，并且当前任务证据确认双方处于同一资源的冲突阶段时，才允许发送一次结构化通知；`idle`、`notLoaded`、已完成、缺失、未知、异资源、兼容阶段或过期快照全部拒绝。
- 新的 `WORKFLOW_COORDINATION_V1` 控制协议绑定 source/target、资源、冲突阶段、owner、lease generation 与 `blocked|released` transition；旧 generation 不能释放新 claim，发送失败后必须重新取得 fresh 活跃快照才可进行唯一一次修正重试，未知宿主回执保持终态 `unconfirmed`，不会机械重复发送。
- fresh source/target 校验、notice 去重、ABA/retry 校验与 pending 占位现在位于同一个会话状态锁事务中；并发相同请求严格只有一条获准，另一条以 pending 重复原因拒绝，避免检查与写入之间的竞态。
- 普通 `send_message_to_thread` 即使含 build、lock、device 或 release 等词也完全不受协调协议拦截；完整入站控制消息只更新有界协调账本，legacy `<codex_delegation>`、混合或畸形控制不会再刷新目标、评估者、计划、执行者、参考或因果合同。
- 状态 Schema 升至 16；活动快照、通知和入站账本只保存域分离指纹、枚举、代次、尝试与时间，不持久化 task/host ID、标题、摘要、资源名或消息原文。新增真实 Desktop `id`/`prompt`/`schemaVersion`、拓扑、歧义清旧、fresh retry、ABA、并发 one-shot、压缩隐私与旧 R06/严格合同回归。

## 1.0.31

- 修复合法高档评估者创建请求被通用 assessor gate 误判为非评估者、进而重复拒绝的问题；任何已识别的 assessor 意图都会先返回具体的绑定、目标、档位、fork、合同或恢复校验原因。Hook 仍只校验和记录请求及宿主回显，不宣称已经切换父会话或子智能体模型。
- 将宿主创建参数归一为一个有界 canonical leaf：兼容直接 `tool_input`、`args`/`arguments`/`input`/`tool_input` 包装、JSON 字符串、有限 list/content block、function/tool 调用包装及宿主工具别名；多 leaf、同字段别名冲突、跨层拼装、超深/超节点/超列表/超 64 KiB 均 fail-closed。
- 同一目标与 assessor binding 的失败重试保持幂等，不刷新 generation、binding、attempt 或类型化失败；重复创建和最多一次实质修正的恢复继续受既有两次尝试上限约束，避免同一矛盾循环。
- Git 挂载保护改用 `exec_command` 实际结构化 command leaf 中的 `workdir`/`cwd`，优先于父 payload cwd；跨 leaf 或冲突路径拒绝，相对路径只按真实 payload cwd 解析，`/tmp` 仅在真实存在目录下放行，并继续阻止 DrvFS/CIFS/UNC。
- 新增 canonical 参数桥接、assessor 具体拒绝/幂等、function wrapper、64 KiB 边界、effective workdir 正反向、跨 leaf 路径、相对路径与挂载路径的 Linux 回归测试；Schema 保持 15，严格计划确认、assessor 首轮只读、唯一执行者、参考验收、因果复核与压缩恢复语义不变。

## 1.0.30

- 新增持久会话偏好 `default|highest_throughout`。只有明确限定本/整个会话、全程或始终，并同时要求最高可用模型和最高推理强度时才启用最高档；同样明确要求恢复默认才退出，偏好跨目标和压缩恢复保留。
- 默认策略保持不变：Daily 继续使用当前策略，Work 继续使用最高档评估者，困难计划确认后继续使用最新较低档、中等推理执行者。`highest_throughout` 只将已确认困难计划执行者改为最高可用档；Hook 仅记录、校验请求和宿主回显，不虚报父会话或子智能体已经切档。
- 明确插件安装或启用不等于 Hook 命令已获信任：命令定义变化后须在实际运行 Codex Desktop 的宿主通过 `/hooks` 或 Hooks 设置审核，只有企业 `managed` 策略可自动信任；插件不写入 `trusted_hash`，也不依赖危险 bypass。
- 新增只读 `hook_trust_doctor.py`，通过 `hooks/list` 报告 9 个 Hook 的启用和信任状态，以退出码 `0`/`2`/`1` 区分健康、需要审核和检查失败；Windows 与 WSL 的命令哈希可能不同，切换 Desktop 宿主后须重新审核。
- 仓库校验同步检查 doctor 的只读安全边界、插件与持久化 writer 版本、README/CHANGELOG 发布版本，并拒绝在发布文档中固化易变的绝对哈希。

## 1.0.29

- 将唯一 Hook 命令生成器纳入插件包本身，正式安装缓存现在可以独立完成九事件命令、精确根目录和确定性生成自检；仓库源码与安装态继续共用同一实现，不新增第二份 Skill 或生成逻辑。
- 新增安装缓存回归门禁，确认从缓存直接运行复杂工作流、Windows 原生 Hook 与 1.0.27 状态迁移时，不依赖仓库根目录文件。

## 1.0.28

- Hook 只执行宿主注入的精确 `PLUGIN_ROOT`；精确 runner 缺失时安全 fail-open，调试模式只输出固定的 `workflow_manager_hook: runner_missing`，不再扫描或执行同级其他版本、marketplace 或稳定 Skill 中未绑定当前信任记录的代码。
- 九个事件的 Unix/Windows 命令改由单一生成器确定性同步，仓库校验只读检查 JSON 漂移、UTF-16LE 编码和精确 resolver；跨平台回归覆盖伪造同级 runner、固定诊断与 Schema 14 的 1.0.27 状态安全迁移。

## 1.0.27

- Work 新目标建立有界高档评估者 binding；Daily 保持当前策略。PreTool/Start/Stop 分别校验请求、回显实际档位和 `WORK_ASSESSMENT` 结果，状态可随压缩恢复且不伪造旧任务已评估。
- Simple 由同一高档评估者自动续接第二阶段实现并验证，无需用户确认；Hard 首轮只能只读并交付严格确认计划，任何提前成功变更都会以 `hard_mutation_before_confirmation` 拒绝。
- 评估失败只允许一次带类型化原因和实质修正的恢复；Schema 13 保留已确认执行合同，未评估 Work 安全迁移为待评估。
- 兼容宿主 Start 仅回显 active model：模型不匹配仍拒绝，缺失 reasoning effort 不伪造观测也不误判失败。
- 评估首轮统一只读；Simple 通过同一绑定评估者的受限 follow-up 执行并以结构化标记收敛。宿主拒绝 spawn 或晚到 Start 不会复活旧合同；明确禁用子智能体的 Hard 计划则使用严格本地执行合同。

## 1.0.26

- 仅在用户明确要求以参考对齐、复刻、一致或视觉/行为保真时启用参考驱动验收；有界参考合同摘要绑定 Hard 执行合同、失效和压缩恢复，绝不保存媒体、原始提示或大输出。
- 参考验收分离工程健康、功能验收、保真候选与用户最终验收；A/B 条件不等、错误场景/方向/版本/状态、过期或静态替代动态均拒绝，客观阈值未获用户授权时 AI 只报告 candidate/差异。
- 修复 collaboration.spawn_agent 宿主归一化载荷：顶层、args、arguments、input 和 JSON 字符串嵌套请求均可被完整合同校验，不再把正确执行者误判为未绑定。
- 压缩/恢复重新注入有界参考状态；单独负向保真反馈也会使旧验收失败，并按是否存在改动基线进入因果复核或替代计划。

## 1.0.25

- Work assessment now uses an objective-bound high-tier assessor lifecycle: request the highest available Codex model and reasoning, preserve real local-high input state only when the parent is already highest, and keep Daily on current settings.
- The same assessor continues Simple work; Hard plans remain strictly confirmed before one lower-tier medium executor runs. Successful handoffs are silent; typed failure is reserved for blocked model or host rejection.
- Hook guards now bridge top-level and nested collaboration spawn payloads so valid bound executor requests are not rejected as unbound.

## 1.0.24

- 已确认计划执行后封存有界基线：只保留前目标、计划、执行合同、改动集和验证证据的指纹/摘要及验收枚举，不保存用户原话、计划正文、命令或子智能体结果。
- 用户在同一会话验收时报告遗留、复现或新症状，必须先结合前目标、已确认计划、执行合同、改动/验证基线、时间顺序和环境变化做只读因果复核；用户表述只是触发条件，不是因果证明。
- 因果结论分为 `introduced`、`fix_ineffective`、`unrelated` 和 `uncertain`，并严格绑定 `baseline_id`、`review_id` 和证据摘要；结论确定前拦截修正性写入、旧执行者和替代执行者。
- `introduced` 或 `fix_ineffective` 要求从整体视角重做困难计划、同时覆盖原验收和新回归面并再次确认；`unrelated` 脱离旧合同重新分类；`uncertain` 继续有界取证，不允许猜测后修改。
- 替代执行合同纳入已解析的因果复核 ID，避免旧计划或旧子智能体在验收问题后继续变更，防止“拆东墙补西墙”。
- 状态 Schema 升级到 11；压缩与恢复保留基线/复核绑定，Schema 10 迁移不会猜测用户已验收或自行生成因果结论。
- 执行者虽结束但没有记录成功改动时，后续验收失败不会虚构“改动导致”的因果关系，也不会被旧成功合同锁死；系统会标记验收失败、清除旧合同并重新进入高推理规划。
- 混合反馈中的“原问题已解决，但修复后出现新症状”仍会进入因果复核，不会被前半句误判为纯验收成功；测试套件同时新增测试方法名唯一性自检，防止 Python 静默覆盖重复用例。
- 回归细节下沉到 `references/regression-continuity.md`，仍只暴露一个可调用 Workflow Manager Skill，主 `SKILL.md` 保持小于 6000 字节。
- Windows 原生端到端测试随因果复核续接覆盖增至 13 项。

## 1.0.23

- 困难工作计划严格确认后新增唯一执行合同：父会话继续以高推理负责协调、合同完整性、恢复决策和最终复核，变更只交给一个合同执行子智能体。
- 执行模型不再硬编码 Luna、Terra 或其他产品名；从宿主当时实际暴露的选项中选择最新的较低档 Codex 模型，显式设置 `reasoning_effort=medium`，并在覆盖模型时使用 `fork_turns=none` 或正整数。
- Hook 不会切换父会话模型；`work_executor_low_latest` 只是策略请求，只有宿主接受带显式模型覆盖的创建请求才算子智能体切换证据。没有合格模型时返回 `model_unavailable`，不静默回退或虚构标识。
- `execution_contract_id` 绑定目标指纹、难度决策 ID、正数 `plan_generation` 与已确认 `plan_digest`；子智能体还必须接收完整计划、独占范围、验收和回退，任何绑定变化都会使合同过期。
- 新增模型不可用、创建配置、创建失败、启动不匹配、合同过期、执行/实现/构建/部署/验证失败等类型化状态。初次失败后只允许一次有实质修正的恢复，总尝试最多两次；禁止原样重试、第二个执行者或父会话接管变更。
- 状态 Schema 升级到 10：Schema 9 的已确认计划迁移为尚未启动的 `spawn_required`，不会猜测旧任务已经创建执行者或完成执行。
- 日常请求、简单工作和既有正收益并行策略保持不变；合同执行细节下沉到 `references/confirmed-execution.md`，继续仅暴露一个可调用 Skill，主 `SKILL.md` 保持低于 5900 字节。
- Windows 原生端到端测试随执行合同往返覆盖增至 12 项。
- 放宽 Windows `cmd -> PowerShell -> Python/失败开放` 完整链路测试的冷启动超时，只修复托管 Runner 性能波动造成的误报，不降低任何功能断言。

## 1.0.22

- 在日常/工作一级分类之后新增独立的简单/困难工作判断；难度不再由 Direct/Focused/Complex/Extensive 或子智能体数量替代。
- `work_assessment` 表达“请求可用最高模型和最高推理强度进行难度分析”的逻辑策略；Hook 只给出可审计建议，不虚报已经切换或验证宿主模型。
- 简单工作问题由评估档直接解决和验证，不额外增加计划确认轮次，同时继续遵守原有破坏性或外部操作确认。
- 困难工作问题允许先以只读方式取证，并要求输出含模块/文件/方法、依赖与所有权、改动、构建部署、验收、风险和回退的详细计划；当前计划严格确认前拦截明确写入、变更型子智能体/Git、构建打包、部署和设备变更。
- 确认绑定计划摘要、目标指纹和难度决策；新增约束、范围/目标变化或重规划请求会使待执行计划失效，兼容证据仍可复用。
- 状态 Schema 升级到 9；详细金集、严格确认及防误拦边界下沉到 `references/work-routing.md`，继续只暴露一个可调用 Workflow Manager Skill，主 `SKILL.md` 保持低于 6000 字节。

## 1.0.21

- 新增独立于 Direct/Focused/Complex/Extensive 的日常/工作一级分类，避免把模型策略和执行形状混成同一个复杂度分数。
- 聊天、天气、日报和电脑清理等日常请求使用 `current` 策略；设备定制、设备 Bug、App/代码开发、构建部署和工程诊断进入 `work_assessment`。
- 混合请求包含不可拆分的工程交付时优先按工作问题处理；日报等明确日常任务不会仅因“生成”一词误判为工作。
- 分类只提供可审计的模型策略建议，不会虚报模型切换，也不绕过删除、覆盖、安装或外发等安全要求。
- 状态 Schema 升级到 8，持久化分类、置信度、稳定规则码和决策指纹；旧状态安全迁移，进度跟进与压缩恢复继承同一领域判断。

## 1.0.20

- 插件包不再通过版本化缓存内的 `skills/` 目录直接暴露 Workflow Manager；Skill 源文件改为普通资产，由安装脚本和 `SessionStart` Hook 同步到 `$CODEX_HOME/skills/workflow-manager` 稳定路径。
- 增加幂等、带受管标记的跨平台稳定 Skill 安装器；内容更新使用同目录原子替换，拒绝覆盖用户自建同名 Skill、符号链接或其他不安全目标。
- 安装和升级流程现在显式执行稳定路径同步，使后续新任务首次发现即使用无版本路径；Hook 同时负责缺失或漂移时自修复，并在同步未验证时明确报告。
- 旧任务原有的版本化注入记录仍不被直接改写，旧缓存继续保留到宿主提供安全迁移能力或相关任务自然结束。

## 1.0.19

- 增加插件升级期间的 Skill 路径连续性规则：仅通过宿主支持的任务迁移 API 重写全部保留任务，禁止直接修改 rollout JSONL、SQLite、索引或活动任务文件。
- 宿主不支持安全全量迁移时，要求后续 Skills 注入改用不含语义版本号的稳定路径，并同时验证新任务与恢复的旧任务均能读取 `SKILL.md`。
- 在全部任务引用完成迁移或稳定路径验证通过前，不再删除旧版本缓存；新版 hook、状态迁移或同级版本回退本身不能作为清理依据。

## 1.0.18

- 新版本首次启动时会安全清理严格更旧的同级语义版本缓存；当前版、更高版、非版本目录和符号链接均保留，异常时失败开放。
- 精炼 Workflow Manager Skill 并把体积门槛收紧到 6000 字节，保留全部质量、路由、委派、连续性和验收约束。
- 修正贡献指南中的 Windows 原生测试数量，并继续覆盖跨版本接管与状态迁移。

## 1.0.17

- 钩子命令在任务绑定的旧版本缓存被清理后，会自动发现并执行最新安装版本，不再要求为常规 Workflow Manager 升级重启 Codex。
- 新 writer 首次运行时会一次性迁移全部保留且有效的会话状态，并通过版本标记避免重复扫描；当前任务随后继续由新 writer 写入。
- Linux/macOS 与 Windows 均增加“旧版本目录已删除、最新版本仍可接管”的端到端测试，Windows 原生测试增至 10 项。
- 三条 `defaultPrompt` 均缩短到 128 字符以内，并在仓库校验与单元测试中固定该宿主限制。

## 1.0.16

- 增强中文“检查、核对、确认、查看”请求的复杂度识别，使全面审计进入 Complex 路由。
- 调整关闭委托门禁时的说明，明确聚焦任务、依赖顺序和共享资源都可能要求串行。

## 1.0.15

- 调度目标改为预期总完成时间/关键路径优先：收益高于协调成本时主动并行，低风险临界情况偏向并行。
- 子任务不再局限于只读分析；独占文件或模块所有权明确时，写代码、测试、研究和复核均可并行。
- Complex/Extensive 的动态子智能体上限分别提升到 2/3，上限仅表示容量，不要求固定派出数量。
- 共享构建服务器、账号、设备和交付链仍受保护，但仅串行化真正冲突的阶段；无关旁路可重新评估并行。
- 增加显式“不使用子智能体”的硬关闭信号。

## 1.0.14

- 将正确性、必要推理、证据、纠错和验收验证设为上下文节省不可逾越的质量门槛。
- 大工具结果默认保留给模型正常处理，取消仅因输出规模触发的硬替换。
- Hook 能识别的构建命令必须保留完整日志和真实退出码，安静模式、输出上限或 head/tail 不再作为替代。
- 压缩检查点增加不含原始内容的验收待办、下一阶段、证据可用性和变更操作指纹。

## 1.0.13

- 修复英文 Windows 系统区域设置下 Unicode 插件路径测试驱动的编码兼容性。
- 保留 `py -3 -m unittest -v tests.test_windows_hook` 九项原生 Windows 端到端验证。

## 1.0.12

- 插件与技能统一命名为 Workflow Manager。
- 顺序任务后续出现独立、只读、可立即开始的工作线时，允许重新评估一次委派。
- 共享设备、构建账号和同一交付物继续强制串行。
- 上下文检查点提前到 70%，压缩后重新启用压力提示。
- 增加 GitHub 仓库市场、中文安装说明和跨平台 CI。

## 1.0.11

- 完善 Windows 九类钩子事件、并发状态写入和失败开放策略。
- 加强压缩续接、重复工作检测与挂载目录 Git 防护。
