# 更新记录

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
