# Abide / 长相守

长相守（Abide）是在原版 P0luz/Ombre-Brain 基础上 fork 并持续二次开发形成的长期记忆系统；在后续设计与优化过程中参考过的其他项目，统一记录在 ACKNOWLEDGEMENTS.md 中。它把值得长期保留的内容存成可检查的 Markdown memory bucket，并在明确的来源、生命周期、隐私和变更边界内完成写入、检索、演化与会话接续。

长相守不是把聊天摘要简单地写入向量数据库。它同时关心：这条记忆从哪里来、是否已经过时、是否应该暂时沉底、谁能看到它、被修改前能否恢复，以及下一次对话应该怎样重新建立连续性。

核心运行不要求 Dashboard、embedding provider、Remember-Me、HTTP 或任何特定消息桥。最小路径是本地 stdio MCP 服务；其余能力按需启用。

## 目录

- [长相守是什么](#长相守是什么)
- [为什么做长相守](#为什么做长相守)
- [功能总览](#功能总览)
- [记忆模型](#记忆模型)
  - [Bucket 不是一条 vector record](#bucket-不是一条-vector-record)
  - [Markdown、metadata 与关系](#markdownmetadata-与关系)
- [生命周期与遗忘](#生命周期与遗忘)
- [情感与浮现](#情感与浮现)
- [检索系统](#检索系统)
- [历史记忆](#历史记忆)
- [Provenance 与事实演化](#provenance-与事实演化)
- [写入、修改与删除安全](#写入修改与删除安全)
- [Todo 与来源](#todo-与来源)
- [会话接续](#会话接续)
- [Boot Profiles](#boot-profiles)
- [Notes、Letters 与 Triggers](#notesletters-与-triggers)
- [Session Archive](#session-archive)
- [相似提醒与冲突检测](#相似提醒与冲突检测)
- [维护能力](#维护能力)
- [Dashboard](#dashboard)
- [导入、导出与备份](#导入导出与备份)
- [Embedding 与外部模型能力](#embedding-与外部模型能力)
- [Remember-Me 图片记忆](#remember-me-图片记忆)
- [MCP 工具](#mcp-工具)
- [系统架构](#系统架构)
- [可选、实验性与 Operator-only 能力](#可选实验性与-operator-only-能力)
- [安全边界](#安全边界)
- [当前限制](#当前限制)
- [License、来源与致谢](#license来源与致谢)
- [安装与快速开始](#安装与快速开始)
- [配置](#配置)
- [接入 MCP 客户端](#接入-mcp-客户端)
- [验证安装](#验证安装)
- [进一步文档](#进一步文档)

## 长相守是什么

长相守为 MCP 客户端提供一个可持久化、可检索、可演化的记忆层。一个 memory bucket 通常包含一段正文、YAML metadata、来源分类、主题/标签、重要度、情感坐标、生命周期状态和关系指针。它可以表示事实、项目上下文、偏好、关系、未完成事项、会话摘要，也可以表示一次反思留下的 feel memory。

记忆以 Markdown 文件保存，metadata 与正文可以直接检查；历史、notes、letters、boot 增量 checkpoint、emotion timeline、embedding index 等辅助状态按功能保存在本地数据目录的 SQLite/JSON 支持文件中。数据目录可以放在 checkout 外，核心不依赖某个托管记忆服务。

长相守还提供三类互补入口：

- MCP：面向模型和自动化工作流的主要接口，默认使用本地 stdio。
- Dashboard：可选的认证浏览器界面，用于浏览、搜索、编辑和维护。
- Remember-Me integration：可选的外部图片/资产记忆集成，不是核心 bucket 存储的替代品。

## 为什么做长相守

常见的“向量记忆”流程可以概括为：写进去 → 生成 embedding → 搜出来。这个流程擅长相似度，却不自动解决长期记忆里的其他问题：

- 同一事实会随时间变化，旧事实不能永远和当前事实拥有同样的权重。
- 摘要、模型推断、系统生成内容和使用者原话不是同一种来源。
- 有些内容需要隐藏，而“搜索不到”不能变成“内容不存在”的侧信道。
- 记忆需要自然沉底和重新浮现，但读取本身不应无意中改变审计结果。
- 一次错误的 merge 或 delete 不应直接毁掉长期积累。
- 新会话需要的是有边界的接续：知道什么值得带入、哪些变化尚未消费，而不是恢复整段旧上下文。

长相守因此把存储、来源、生命周期、事实演化、破坏性操作安全和连续性组合在同一套模型里。它不替模型做最终判断：相似提醒不会自动 merge，冲突检测不会自动决定哪个事实为真，feel 提示也不会强迫产生新记忆。

## 功能总览

| 能力 | 当前状态 | 简介 |
| --- | --- | --- |
| Markdown bucket 记忆 | 核心 | 正文与 YAML metadata 分离保存，可直接检查和备份。 |
| 生命周期与遗忘 | 核心 | activation、decay、dormant、resolved、archive、sealed 共同控制记忆状态。 |
| 情感坐标与 feel | 核心 | 使用 valence / arousal 连续坐标，支持 feel memory 和 emotion trend。 |
| 关键词/模糊检索 | 核心 | 不配置外部 provider 也能运行的 lexical/fuzzy recall。 |
| 语义检索与 related | 可选 | 使用 OpenAI-compatible embedding provider 生成向量、做 semantic recall 和关系维护。 |
| Provenance 与 supersession | 核心 | 区分来源类型，并显式表达旧事实与 successor 的关系。 |
| 历史正文读取 | 核心 | write-ahead snapshots 支持 breath(as_of=...) 的只读历史检索。 |
| 安全写入与删除 | 核心 | similarity/conflict warning、短时一次性确认 token、快照与指针安全。 |
| boot 连续性 | 核心 | pinned context、delta、triggers、notes、letters、todos、sessions 和 feel echo 的有界组合。 |
| Notes、letters、triggers | 核心 | 区分使用者留言、会话交接信和到期记忆，不混入普通 bucket 检索。 |
| Session archive | 核心 | 保存摘要、highlights、mood、情感坐标、topics 和可选 handoff letter。 |
| Maintenance | 核心/受控 | pulse、dream、digest、related_backfill 提供状态、反思和维护路径。 |
| Dashboard | 可选 | 认证浏览器 UI，提供记忆桶、归档、搜索、网络、导入、配置和图片资产界面。 |
| 对话导入 | 可选 | 支持多种对话导出格式，分块、暂停、恢复并提供导入后 review。 |
| 普通 portable export | 可选 CLI | 导出未封存的普通记忆数据；不是完整 disaster recovery。 |
| Remember-Me 图片记忆 | 可选外部集成 | 隐私清理后的图片资产、metadata、检索、展示和下载。 |
| GitHub/OIDC 与加密 backup | Operator-only | 需要明确 authority 和独立部署审查的高级备份路径。 |
| Raw Evidence、诊断 probes | Operator-only/开发诊断 | 默认关闭，不进入普通用户 workflow 或 model context。 |

## 记忆模型

### Bucket 不是一条 vector record

Bucket 是一条有正文、有当前状态、有来源、有关系、有生命周期的长期记录，而不是一个只有 id + vector 的结果项。它的主要维度包括：

- domain：主题域，例如项目、工作、关系或 session；它是结构化过滤和 boot profile 的重要输入。
- tags：可复用的层级化标签；标签是约定，不是一个固定的 schema 枚举。
- importance：1–10 的重要度，用于排序和 decay；pinned 会锁定为高重要度语义。
- pinned 与 protected：需要长期保留的边界。pinned 可由普通工具设置/取消，取消需要确认；protected 是更高的受保护状态，不由普通工具任意设置。
- resolved：事项是否已经处理；它会使记忆沉底，但不等于删除，也不等于 superseded。
- digested：是否已经经过反思/消化流程的标记，不应被理解为正文已经消失。
- dormant：是否暂时不主动浮现；读取和显式 wake 的语义彼此区分。
- sealed：内容是否处于默认不可见边界；普通 recall、统计和导航不会用侧信道暴露它。
- related_buckets：双向的相关记忆关系；它表示有关联，不表示一方取代另一方。
- superseded_by / supersedes：事实演化的正向和反向指针，见后文。
- trigger_date：prospective memory 的到期日期，在合适的 boot 中浮现。
- provenance_kind：正文来源分类；它不会因为内容“看起来像原话”就自动升级。

### Markdown、metadata 与关系

每个普通 bucket 的正文是 Markdown，metadata 位于 YAML frontmatter。正文负责保存可阅读内容，metadata 负责描述当前状态、排序所需字段、来源和关系。二者不是互相替代的副本：只修改 metadata 时不会制造正文历史版本；替换或追加正文时，旧正文会先写入 history。

在启用相应配置时，系统可以根据主题、标签或关键词维护 Markdown wikilink；Dashboard 还会识别正文中出现的 bucket ID、related reference 和反向引用，并提供跳转。链接是导航辅助，不是事实来源；真正需要细节时仍应读取被引用的原 bucket。

feel memory 是专门的情感/反思记录。它可以通过 hold(feel=True, source_bucket=...) 关联到触发它的记忆，带有自己的 valence/arousal，但默认不参加普通记忆浮现。它不是把“模型感觉”伪装成使用者原话。

## 生命周期与遗忘

长相守把“保存”与“永远主动出现”分开。记忆的活跃程度由 activation、最近活动时间、重要度、情感坐标、resolved/digested 状态和保护状态共同影响。

### 从活跃到沉底

普通检索或 surfacing 可能更新有限的 activation metadata；touch=False 的读取则明确禁止 activation、last_active、dormant 唤醒、dehydration cache 和 decay/dormant marking 写入。后台 decay 在首次需要时启动，按当前配置检查动态 bucket：低活动记忆可以被压缩、自动 resolve，或移动到 archive。archive 是保留状态，不是物理删除。

低活跃记忆可以进入 dormant，普通 recall 不会主动浮现；它仍然可以通过 include_dormant=True 读取。只有显式 wake_dormant=True 或 trace(dormant=0) 才表达唤醒意图；没有隐式 wake 的保证。

### 哪些记忆不会按普通规则衰减

pinned、protected、永久类型和 feel memory 受 decay 保护；sealed 内容不会被普通 decay 统计或暴露。未完成 todo、重要度和活动状态会影响系统是否把一个 bucket 视为可沉底对象。resolved 会降低浮现优先级，resolved + digested 会进一步加速淡出，但仍然不是删除。

pulse、dream 和 breath 的 touch 行为不同：普通列表/浮现可能触发有限的生命周期更新，维护审计应使用 touch=False；as_of 历史检索无论传入什么 touch 值都保持只读。

## 情感与浮现

### 情感坐标

记忆和 session 可以保存两个连续的情感坐标：

- valence：从 0 到 1 的效价坐标。
- arousal：从 0 到 1 的唤醒度坐标。

它们不是固定的“开心/难过”枚举。写入时可以显式提供，也可以让可选 provider 做分析并在失败时使用实现规定的 fallback。检索可以按坐标过滤，或用 resonance="valence,arousal" 查找与目标情感位置接近的记忆。

### Feel、趋势与浮现

hold(feel=True) 记录的是反思者对某条记忆的感受，不是事件本身的客观情绪；source_bucket 可记录它来自哪条记忆。系统还会保留有限的 emotion timeline，breath(emotion_trend=True) 可以把趋势附加到检索结果。

未解决、高重要度、被保护或情感权重较高的内容更有机会在 surfacing 和 boot 中优先出现。这个优先级不是“情绪越强就一定被召回”，仍会受到 query、过滤器、阈值、token budget 和 sealed/dormant 边界限制。

## 检索系统

breath 是面向检索的主要工具，既支持定向 query，也支持没有 query 的有限 surfacing。它先按可见性和结构化条件筛选，再综合 lexical/fuzzy、可选 semantic、主题/内容、情感 resonance、时间邻近度和 importance 排序；pinned/protected 与 supersession 也会影响结果顺序。

### 定向检索与无 query 浮现

- query 可以直接使用自然语言，不要求先转换成关键词。
- 没有 query 时可以按当前实现的 surfacing 规则浮现，不等价于“把整个库列出来”。需要系统状态和列表时使用 pulse。
- max_results 控制结果数量，max_tokens 控制输出预算；mode="summary" 是紧凑摘要，mode="full" 才请求正文级内容。
- 结果会说明 bucket ID、通道/分数和必要的 truncation 信息。输出被截断时会保留可续读的 ID 与 dream(detail_ids=...) 入口，而不是把部分正文伪装成完整正文。

### 过滤、分页和安全读取

breath 当前支持的主要过滤/控制项包括：

- tags_filter、topic_filter、importance_min；其中 topic_filter 主要用于 session archive 的主题。
- recent_days、date_from、date_to。
- valence、arousal、resonance 和 feels。
- include_dormant、wake_dormant、include_sealed。
- touch=False：维护、审计和验收读取应使用它，避免改变 activation/lifecycle metadata。
- cursor：不透明的 query 分页游标；应与同一组 query/filter 一起复用。
- mailbox=True：读取 mailbox/letter 通道，而不是把 letter 当作普通 bucket 搜索。

普通结果默认不显示 sealed 内容。即使显式使用 include_sealed，也应把它理解为受当前调用边界约束的维护能力，而不是关闭 sealed 安全模型。

### 语义检索与 fallback

配置 embedding 后，系统可以将 query 和 bucket 内容映射到本地 embedding index，执行 cosine semantic recall，并给结果标记 semantic 通道。没有 embedding、provider 不可用、索引为空或模型不一致时，核心仍可使用 lexical/fuzzy 路径；相似提醒、related 回填和某些维护提示会明确报告“检查未执行/不可用”，不会把不可用说成“没有相似结果”。

## 历史记忆

正文发生替换、追加或其他破坏性内容变化前，BucketManager 会把旧正文写入 write-ahead bucket_history snapshot。它既支持 operator 手工恢复所需的历史材料，也支持 breath(as_of=...) 在历史时间点进行只读 keyword/fuzzy 检索。

as_of 有明确边界：

- 必须和 query 一起使用；历史浮现、wake_dormant、当前时间过滤、tags/topic filter、mailbox 和 historical semantic search 不在这条路径中。
- 只对仍存在且当前可见的 bucket 重建正文；它不是删除后完整重建，也不是整个数据库的历史快照。
- 返回的是历史正文，但 metadata 仍然是当前 metadata；结果会标注“历史版本 / as_of / metadata=当前”。
- 历史 snapshot 没有历史 metadata，因此不能用今天的 provenance 标签冒充过去的 metadata；历史路径也不附加当前 emotion trend。
- cursor 会冻结 as_of 和 touch 模式，翻页不能换时间点或改变只读性。

因此，as_of 是“在一个时间点查看仍存在 bucket 的历史正文”，不是版本化的全量 memory database。

## Provenance 与事实演化

### Provenance：正文从哪里来

当前 provenance_kind 只有四类：

| 值 | 含义 | 使用边界 |
| --- | --- | --- |
| unknown | 没有可信来源分类 | 不能因为内容像原话就升级为使用者陈述。 |
| summary | 压缩、改写或总结 | 不能直接当作原始引文。 |
| inference | 模型推论、反思或判断 | 不能说成使用者明确说过。 |
| system | 系统或维护流程生成 | 不代表使用者陈述。 |

当前行为会按写入路径产生默认分类：普通 hold 通常是 unknown，grow 和 session archive 产生 summary，feel 产生 inference，digest log 产生 system，import 不凭空制造更强来源。修改正文而没有显式重新指定来源时会回到 unknown；只改 metadata 会保留原值。

普通 breath 会带 source-aware 的 [prov=...] 标记。真正的原话应来自 notes、当前对话原文或明确提供的来源材料，而不是来自 summary、inference 或 system bucket。

### Supersession：事实会变化

长期记忆不是静态事实库。同一事实发生变化时，长相守同时保留“曾经如此”和“当前应当如此”的关系，而不是静默覆盖所有历史。

- hold(supersedes_id=...)：同一 bucket 原地演化，bucket ID 不变；旧正文进入 history，只改正文，不自动改 tags、importance 或 pinned。
- trace(superseded_by=...)：跨 bucket 建立取代关系。旧 bucket 保留并可被检索，但检索权重降为原来的约 0.1，并显示 successor。
- superseded_by=""：撤销已有 supersession。
- superseded_by="none"：整条 bucket 已作废，但没有 successor；这不是 no-op。
- superseded_by="bucket_id"：指向明确的 successor；系统同时维护反向 supersedes 列表。

系统拒绝 self-link、缺失目标和 sealed successor，并在重连、merge、delete 时维护两边指针：被 inbound 指向的 bucket 不能被直接删除；merge 删除 source 时会清理 outgoing reverse 并让其他指向 source 的关系重连到 target。不能唯一确定 successor 时不猜，应该建立带时间状态的新记忆或先人工审计。

## 写入、修改与删除安全

### 写入能力

| 工具 | 用途 |
| --- | --- |
| hold | 保存一条有未来价值的记忆；支持 tags、importance、pinned、valence/arousal、feel、source_bucket、trigger date、supersedes_id 和 provenance。完全相同文本才会确定性复用；语义相近不会自动合并。 |
| grow | 处理长内容或日记，把多个独立事件/主题拆成多个 bucket；短内容走快速路径。它可能需要 provider 做分析、拆分、摘要或 merge。 |
| trace | 统一修改入口：正文替换/追加、name、domain、tags、importance、todo、resolved、pinned、digested、dormant、sealed、trigger date、provenance、related、unrelate、supersession、merge 和 delete。 |
| refresh_tg_summary | 只更新 pinned bucket 的 tg_summary metadata，不改正文；必须携带 boot(tg) 返回的 source hash，正文变化时拒绝写入。 |

trace 支持按逗号分隔的多 bucket 操作，但批量操作逐条执行、不是原子事务，可能返回部分完成；批量模式会拒绝不能安全批量解释的字段。修改正文不会自动重算 todo；如果任务语义变化，必须显式更新 todo。

### 相似提醒和冲突检测不会替你做决定

hold 之前可以进行只读相似提醒：embedding 可用且相似度达到当前门槛时，返回候选 bucket 和分数。它只是 warning：不会自动 merge、建立 related 或 supersede。

如果启用 conflict detection，系统先用 lexical/semantic recall 找候选，再让 provider 返回结构化的 same_fact、conflict、bucket_id 和新旧正文证据。只有“同一事实槽位”且确实互斥才算冲突；同一主题、同一年或关键词重合不够。provider 不可用、返回无效或没有配置时，写入仍可成功，但会明确报告“检查未执行”，不能解释成“没有冲突”。

### 破坏性操作的两阶段保护

trace(delete=True) 不是一次调用直接删除：

1. 第一次调用只生成目标预览和短时 confirmation token。
2. token 绑定具体 bucket、当前计划和状态，并且一次性、会过期。
3. 只有在明确确认后，以相同目标携带 token 重试，才会在写入 history 成功后执行删除。

MCP 没有 undo/restore 工具；write-ahead history 是安全网和受控恢复材料，不是一个对普通客户端开放的撤销按钮。pinned 取消、digest 的写入维护也遵循显式确认语义；related_backfill 默认 dry-run。Dashboard 的写操作另受 Dashboard 认证和写入保护，不能把它理解为 MCP delete token 的替代品。

sealed bucket 的正文修改默认拒绝，protected bucket 的删除、正文修改和受保护 importance 修改会拒绝。sealed 的存在性也尽量隐藏：默认查询、统计、related 和 successor 展示不能通过“未找到/数量变化”泄露它。

## Todo 与来源

长相守同时兼容旧的 todos: list[str] 和带出处的 todo_items。后者的每一项可以包含：

- text
- said_by
- said_at
- source_bucket

todo_provenance 会与 todo 文本做 reconciliation；合并 bucket 时，系统会保留已知出处，不能把未知出处升级成确定事实。todos(include_provenance=True) 才按出处分组；普通 boot、breath、pulse、dream 不默认展开这层来源。

said_by 必须使用当前 schema 的原值：ting、model、system 或 unknown。ting 是现有 compatibility literal，不应擅自改成未声明的 user；只有确实知道来源时才使用非 unknown 值。不要编造 said_at，也不要因为“这是模型抽取的”就把任务标记成某一方明确提出。

todo 的单项完成和整个 bucket resolved 是两件事；未完成 todo 会影响 lifecycle 和维护检查。

## 会话接续

长相守的连续性不是把完整历史重新塞回新上下文，而是每次开窗口时重新组合一份有预算、有来源、有状态的入口。boot 会从持久化记忆中组合：

- 当前 profile 允许的 pinned/protected context；
- 到期的 trigger；
- notes 和 mailbox 中可见、可递送的内容；
- 自上次该 profile 成功 checkpoint 之后的 delta；
- 最近 session archive；
- 未完成 todos；
- talk profile 的 feel echo；
- 必要的 truncation/omission 说明和稳定 ID。

每个 profile 都有独立的 boot-delta checkpoint。一次成功的 boot 才推进该 profile 的 checkpoint；失败不会吞掉变化。talk 的成功 boot 不会消费 code 或 tg 的 delta，三个 profile 也不会因为切换而重复投递同一条一次性 note。

这意味着新会话得到的是“当前值得接续的结构化入口”，不是一份假装完整的旧 transcript。需要正文时，按 bucket_id 使用 breath(mode="full") 或 dream(detail_ids=...) 继续读取。

## Boot Profiles

当前有三个 profile。它们共享同一套事实、sealed、provenance、supersession 和生命周期语义，差异在于筛选策略、章节、输出预算和 checkpoint。

| profile | 最大 token | pinned 预览上限 | delta 预算 | trigger | mailbox | session | feel echo |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| talk | 16000 | 5000 字符 | 600 | 10 | 1 | 最近 3 次 | 开 |
| code | 12000 | 2500 字符 | 400 | 10 | 1 | 最近 3 次 | 关 |
| tg | 4000 | 600 字符 | 220 | 5 | 1 | 关 | 关 |

tg 只是一个紧凑的 channel profile，不包含 Telegram transport，也不包含私人 bridge。对于 pinned bucket，它可以使用由调用方生成的 tg_summary；summary 以正文 SHA-256 校验，缺失或过期时回退到带透明度的预览。refresh_tg_summary 不自行调用外部 LLM，正文始终是唯一真实来源。

tg 会尽量报告被省略 item 的稳定 ID、显示/总长度和继续读取方法。code 的 session/pinned 内容仍可能按预算机械截断，不能把它描述为与 tg 完全相同的截断透明度。

## Notes、Letters 与 Triggers

### Notes：使用者留言

leave_note(text, sealed=False, open_at="") 保存一条独立 note。它不是 memory bucket，不进入 embedding、breath、digest、dehydration 或 decay，适合记录需要在后续 boot 传递的原话。

- list_notes 按时间列出可见 note，get_note 按整数 note_id 读取全文。
- sealed note 和尚未到 open_at 的 note 默认连存在性都不透露。
- boot 最多自动展示一条符合条件的新 note，并为一次性投递记录状态；较旧 note 仍可按 ID 查询。
- 如果正文超过 boot 预算，boot 会提示使用 get_note，不会把截断内容冒充全文；成功读取全文后才结束这条 note 的自动递送状态。

### Letters 与 mailbox：会话交接

archive_session 可以同时写入一封 handoff letter。letter 位于独立 SQLite 表，带有 letter_id、创建时间和关联 session ID；get_letter 读取全文，seal_letter 修改 letter 的可见性，breath(mailbox=True) 或 boot 读取可见 mailbox 内容。

letter 不是普通 bucket，也不是 note：它表达的是一次会话结束时给未来窗口的交接。sealed letter 默认按“letter_id not found”处理，避免通过错误类型泄露存在性；需要明确授权时才使用 include_sealed=True。

### Triggers：prospective memory

hold(trigger_date="YYYY-MM-DD") 为记忆设置到期日期。到期记忆会在 boot 的 trigger section 中浮现，并记录 trigger_last_seen，而不会变成外部通知任务。它依赖下一次 boot 才能被看见；长相守本身不承诺操作系统通知、消息推送或独立 scheduler。

## Session Archive

archive_session 将当前会话保存为一个独立的 archived session bucket，可以提供：

- summary 和 highlights；
- mood、valence、arousal；
- 3–8 个适度范围的 topics；
- 可选 handoff letter；
- sealed=True，将摘要和交接信放入更严格的可见性边界。

session archive 的正文 provenance 是 summary。归档后 session bucket 仍可修改：未 sealed 的可以通过 trace 修正/追加，sealed 的正文修改会拒绝。Dashboard 将归档对话独立分页展示，boot 默认带最近 3 次归档摘要；topic_filter 可以用于归档主题筛选。

## 相似提醒与冲突检测

这两种机制解决的是不同问题：

| 机制 | 判断 | 不做什么 |
| --- | --- | --- |
| 相似提醒 | 新内容是否和可见旧 bucket 在语义上接近；可使用 embedding，相似度达到门槛时给出候选。 | 不自动 merge、不自动 related、不自动 supersede、不阻止 hold。 |
| conflict detector | 在候选足够相关时，是否是同一事实槽位的互斥陈述；返回真实新旧文本证据。 | 不决定哪个事实“是真的”，不把不同时间的自然演化当作冲突。 |

相似候选 recall 会遵守普通可见性，跳过 sealed 和 dormant。embedding provider、conflict provider 或返回格式不可用时，结果会是 unavailable/“检查未执行”，不会 fail-open 地给出“没有冲突”的结论。冲突检测是可配置的 provider-backed 能力，属于写入前的 warning，不是自动事实裁判。

## 维护能力

普通读取和维护工具有不同的写入风险，应明确区分：

### pulse

pulse 报告 permanent/dynamic/archive 数量、存储大小、decay 状态和记忆桶列表。默认重点显示 pinned/protected 与 dynamic top 结果；show_all=True 才走有界的全部列表，limit 最大 50，offset 支持分页。需要纯查看时传 touch=False。

pulse(health=True, touch=False) 是只读维护体检，当前检查无名 bucket、无标签 bucket、陈旧 todo bucket、supersession 链完整性和 pinned 但重要度过低等问题。它不启动 decay、不标 dormant、不唤醒 bucket，也不统计 sealed 内容。

### dream

无参数的 dream 读取最近的少量、未封存、未 pinned/protected、未 dormant 的动态记忆，提供可选的关系/feel 反思提示；它不是每次启动都必须调用的步骤。dream(detail_ids="id1,id2") 可以读取指定 bucket 的全文；wake_dormant=True 只有在明确指定时才表达唤醒。dream 的 surfacing 会更新有限的 activation metadata，因此不把它称为纯只读。

### digest

digest(dry_run=True) 规划旧 bucket 的自动消化和 importance rebalance；真正执行前先展示候选和获取绑定计划的一次性确认 token，执行可能创建 summary bucket、写入 digest log、标记来源 bucket，并产生 provider/API 成本。mode="dedupe" 是本地 embedding 查重扫描，只读、不改数据，可选择是否包含 archive。

### related_backfill

related_backfill 根据 embedding 为未封存 bucket 规划双向 related 关系，默认 dry_run=True，可设有界 limit 和阈值。执行模式会写关系、跳过 sealed；不应在没有明确维护意图时主动执行。

## Dashboard

Dashboard 是可选的认证浏览器界面，不是核心 MCP loop 的必要条件。当前界面包含：

- **记忆桶**：按 score 浏览 active buckets，查看 name、domain、tags、valence/arousal、importance、resolved、pinned、activation 和 content preview。
- **归档对话**：把 session archive 与普通 bucket 分开，按查询、limit/offset 分页浏览。
- **搜索**：普通相关搜索之外，还支持按 bucket ID/ID 前缀、name、正文引用和 related reference 分组查看。
- **详情与参考导航**：详情页显示正文、metadata、score、正文中出现的 bucket links，以及引用当前 bucket 的 referenced_by；可以在 related、正文引用和 supersession 指针之间跳转。浏览器历史支持详情页前进/后退。Dashboard 不提供 bucket_history 时间线；历史正文使用 MCP 的 breath(as_of=...)。
- **记忆编辑**：认证后的详情页可以编辑 bucket 正文或删除 bucket。该界面不提供普通用户任意修改所有 frontmatter 字段的能力；MCP 的 trace 仍是完整记忆变更入口。
- **Breath 模拟**：展示 query、valence/arousal、候选池、维度评分、阈值和排序的可视化检索链路，便于检查当前 recall 行为。
- **记忆网络**：在 embedding 可用时显示 bucket 相似关系网络；无向量时相关视图可能为空或不完整。
- **导入**：上传对话文件，显示分块进度、API 调用、新建/合并/保留原文计数，支持暂停、结果 review 和高频模式检测。
- **配置**：查看/调整 provider、embedding 和运行时参数；API key 仍应通过环境变量管理，不能提交到仓库。
- **设置**：主题、服务状态、Dashboard 密码变更、宿主机数据目录提示和退出登录。
- **图片资产**：启用 Remember-Me 后，展示分页/筛选的图片库，支持上传、详情、metadata 编辑、下载和删除等认证 UI 操作。

Dashboard 的认证/首次设置、会话 cookie、写入保护和 CSRF 边界只适用于 Dashboard/HTTP surface；最短的本地 stdio 路径不需要启动 Dashboard。

## 导入、导出与备份

### 对话导入

Dashboard 导入工作流会把来源对话标准化成 turns，再按约 10k token 窗口分块处理。当前解析路径覆盖 Claude JSON、ChatGPT export、DeepSeek 风格对话文本、Markdown 和 plain text；格式探测按当前导出结构执行，不承诺所有平台的每个历史版本都能无损解析。

导入支持：

- import_state.json 记录进度；暂停后可以 resume，源文件 hash 变化时不会盲目接续旧分块。
- preserve_raw 只表示保留“提取出的 memory item 内容并跳过后续 merge/dehydration”；它不保存上传文件原始字节、不可变 transcript、精确 quote/span 或完整来源证据。
- 导入后列出新建/合并结果，并可在 Dashboard 中标记重要、pin、noise 或删除。
- 可选 embedding pattern detection 用于发现高频主题，结果需要人工 review。

导入通常需要 provider 做 memory extraction；不要把导入和无 provider 的最小 hold 路径混为一谈。Raw Evidence capture 是单独的 operator-only 边界，见后文。

### 普通 portable export

portable_export.py 提供 ordinary portable export CLI，而不是 MCP tool。它需要显式确认，并在受控写入冻结下构造一个新目录；源数据在过程中变化时 fail closed，不发布半成品。

普通导出包含可见、未 sealed 的 ordinary memory 数据：当前 bucket 正文和 metadata、可见 bucket 的 write-ahead body history、未 sealed 的 notes 与 letters、emotion timeline，以及本地 ordinary asset 的 metadata/blob（若存在）。关系和 todo provenance 中指向不可见对象的部分会被清理。

它明确不包含：sealed bucket 及其 history/letters/notes、Remember-Me external authority、Raw Evidence、embedding/dehydration cache、临时文件和 WAL/SHM、Dashboard authentication/config/secrets、import state/journals、boot-delta checkpoints 等。因此 portable export 不是完整 disaster recovery，也不等于 RM 或 operator backup。

### 高级 backup authority

GitHub/OIDC backup 是可选的 operator surface，不是普通 MCP workflow。authority 必须通过显式 OMBRE_BACKUP_REPOSITORY 配置；缺失、格式错误、repository/ref/workflow/event 不匹配都会 fail closed。它不绑定 peanutsuee/Abide，也不绑定任何未显式配置的仓库。

当前还包含独立的加密、offline-quiesced backup bundle/restore 路径，要求 operator 提供独立 workspace、收件公钥/指纹、OIDC 约束和运行限制。restore 会先验证 bundle，再发布到新的目标 root，不替换正在运行的 live root。它们都不进入 Minimal Mode，也不应写进普通使用者的 MCP 配置示例。

## Embedding 与外部模型能力

embedding 是可选的。没有 embedding key 或 provider 时，Abide core 仍可以用 Markdown bucket、lexical/fuzzy retrieval、hold、trace、boot 和其他核心工具运行。

启用后，embedding index 存在数据目录中，主要用于：

- breath 的 semantic recall；
- hold 前相似提醒；
- related_backfill 和新 bucket 的 best-effort related linking；
- dream 的关系提示和 feel 聚类提示；
- digest(mode="dedupe") 的本地查重；
- 导入后的高频模式检测。

写入和 embedding 可以分别配置。当前实现通过 OpenAI-compatible API 访问外部 provider，模型、base URL 和 key 不是 README 固定的唯一方案；可使用 .env.example 中的 OMBRE_EMBEDDING_* 配置。provider 请求失败时，普通核心写入不会因此自动变成不可用；语义通道会报告不可用并回退到 lexical/fuzzy，具体 provider-backed 操作仍可能明确失败或跳过。

同样，dehydration、长内容拆分、自动打标、冲突检测、digest 和 import 都可能需要独立的 provider 配置并产生 API 成本。Minimal Mode 不以这些能力为前提。

## Remember-Me 图片记忆

Remember-Me 是长相守的可选 external integration，用来保存与检索经过隐私清理的图片/资产；它不是 Abide core 的 bucket storage，也不是普通 memory 的隐式附件层。

它的 source 不 vendored。只有明确启用 asset/image integration 时，才安装 requirements-remember-me.txt，并给它独立的数据根目录。核心启动、普通 Markdown 记忆、breath、boot 和最小 stdio 路径都不要求 Remember-Me。

### 当前兼容边界

下列状态只针对 Remember-Me integration，不是 Abide core MCP 的客户端限制：

- Claude：**SUPPORTED / TESTED**
- ChatGPT：**NOT CURRENTLY SUPPORTED**。曾有一次真实连接，但出现了尚未诊断的兼容问题；不猜测原因，也不声称当前可用。
- 其他 MCP / LLM client：**UNVERIFIED**

### 用户可使用的资产能力

启用 integration 后，当前公开工具和 Dashboard 资产库支持：

- 生成短期 upload link、查询 metadata-only upload status；
- 获取 asset metadata，更新 title、description、tags，而不改动文件 bytes/hash；
- keyword、tag/filter、kind/mime/date 和可选 semantic asset search，支持 limit/offset；
- rm_asset_view 向当前使用者展示隐私清理后的图片；
- rm_asset_inspect 向模型返回已清理的 MCP ImageContent，用于真实视觉理解；不能根据 metadata 猜图；
- 生成短期 signed download link，作为显式下载/导出动作；
- 对缺失或过期的 asset embeddings 做有界 reindex，不修改资产 bytes 或 metadata；
- 通过 content-addressed storage 保存清理后的内容，并在适用路径执行 metadata stripping；
- 在 Dashboard 图片库中分页、筛选、上传、查看、编辑 metadata、下载和执行认证后的资产管理操作。

普通 MCP surface 没有 asset delete tool；图片删除属于受认证的 Dashboard/API 资产管理边界。Remember-Me 的 blob authority 与 Abide ordinary portable export 分开，不能把二者当作同一份备份。

## MCP 工具

当前源码注册的默认 surface 是 **26 个 MCP tools**：17 个核心记忆/维护工具和 9 个 Remember-Me 资产工具。启用 OMBRE_DIAG_TOOLS 后，额外注册 15 个开发/验收诊断工具，当前总数为 **41**。数量是当前版本观察值；客户端实际能力应以运行时 MCP schema 为准。

### 默认工具

| 工具 | 类型 | 作用与关键边界 |
| --- | --- | --- |
| boot | 普通读取/连续性 | 按 talk、code 或 tg 生成有预算的启动上下文；成功后推进对应 profile checkpoint。 |
| breath | 普通读取/检索 | Query、surfacing、结构化过滤、semantic/lexical ranking、分页、历史 as_of 和 touch/sealed/dormant 边界。 |
| dream | 普通读取/反思 | 最近记忆摘要或按 ID 读取全文；wake_dormant 需显式指定，surfacing 可能更新 activation。 |
| pulse | 状态/维护读取 | 系统状态和 bucket 列表；show_all、limit、offset 有界，health 必须用 touch=False。 |
| hold | 写入 | 创建单条 bucket 或 feel；写入前可提示相似/冲突，但不自动 merge、related 或 supersede。 |
| grow | 写入/整理 | 将长内容拆成多个 bucket；provider-backed 分析、摘要或 merge 可能产生外部 API 成本。 |
| trace | 修改/破坏性 | 正文、metadata、todo、关系、supersession、merge、seal、dormant 和 delete 的统一入口；delete 需要绑定计划的一次性 token。 |
| archive_session | 写入/归档 | 创建 archived session bucket，可附 highlights、mood、情感坐标、topics 和 letter。 |
| get_letter | 精确读取 | 按 letter_id 读取 handoff letter；sealed letter 默认不可见。 |
| seal_letter | 维护写入 | 改变 letter 的 sealed 可见性；不是普通检索。 |
| leave_note | 写入/连续性 | 保存独立 note，不创建 bucket；支持 sealed 和 open_at。 |
| list_notes | 读取 | 列出可见 note；sealed/未到期 note 默认隐藏。 |
| get_note | 精确读取 | 按 note_id 读取全文；成功读取会记录 note 的 read/delivery 状态。 |
| todos | 普通读取 | 汇总未 resolved、未 sealed bucket 的 todo；include_provenance=True 才展开出处分组。 |
| refresh_tg_summary | 受控写入 | 仅写 tg_summary metadata；必须提交当前正文 SHA-256，正文变化时拒绝保存。 |
| digest | 维护 | maintenance 默认 dry-run 并需确认；dedupe 是本地只读 embedding 查重。 |
| related_backfill | 维护 | 默认 dry-run 的 semantic related 回填；执行模式写双向关系并跳过 sealed。 |
| rm_asset_upload_link | Remember-Me 可选 | 生成短期 asset 上传 link；上传由服务端校验大小/hash。 |
| rm_asset_upload_status | Remember-Me 可选 | 查询持久 asset 上传状态，只返回 metadata，不返回 bytes。 |
| rm_asset_get | Remember-Me 可选 | 按 asset ID 获取 metadata，不返回文件 bytes 或磁盘路径。 |
| rm_asset_update_metadata | Remember-Me 可选 | 更新 title、description、tags，不改变 bytes/hash。 |
| rm_asset_search | Remember-Me 可选 | 关键词、tag/filter、日期和可选 semantic asset search，支持分页。 |
| rm_asset_reindex_embeddings | Remember-Me 维护 | 有界回填缺失/过期 asset embedding，不修改资产本身。 |
| rm_asset_download_link | Remember-Me 可选 | 生成五分钟级别的 signed download link，是显式导出动作。 |
| rm_asset_view | Remember-Me 可选 | 面向使用者展示隐私清理后的图片，必要时返回 signed-link fallback。 |
| rm_asset_inspect | Remember-Me 可选 | 向模型提供已清理的图像内容；不更新 metadata 或 embedding。 |

### 其他 MCP surface

除 tools 外，当前还注册：

- remember-me-asset-viewer resource：用于 inline 展示单个已清理 Remember-Me 图片。
- start_ombre_brain prompt：可选 onboarding guidance；不支持 MCP prompt 的客户端仍可直接使用 tools。

精确的参数、类型、默认值、枚举和 schema 以运行中的 MCP discovery 为准；行为与安全说明集中在 [docs/ABIDE_GUIDE.md](docs/ABIDE_GUIDE.md)。README 不复制完整 JSON schema。

## 系统架构

~~~text
┌──────────────────────────────────────────────────────────┐
│ MCP Client                                               │
│  stdio（默认） / authenticated HTTP（可选高级路径）     │
└──────────────────────────┬───────────────────────────────┘
                           │ MCP tools / resource / prompt
                           ▼
┌──────────────────────────────────────────────────────────┐
│ Abide MCP Server                                         │
│  boot / breath / hold / grow / trace / maintenance       │
│  notes / letters / session archive / safety boundaries    │
└──────────────┬───────────────────┬───────────────────────┘
               │                   │
               ▼                   ▼
┌────────────────────────┐  ┌────────────────────────────┐
│ Memory / lifecycle      │  │ Retrieval                  │
│ bucket model            │  │ lexical + fuzzy            │
│ provenance              │  │ + optional semantic        │
│ supersession            │  │ ranking / filters / cursor │
│ decay / dormant / seal  │  └──────────────┬─────────────┘
└──────────────┬─────────┘                 │
               ▼                           ▼
┌──────────────────────────────────────────────────────────┐
│ Local storage                                             │
│ Markdown + YAML buckets / SQLite history, notes, letters, │
│ checkpoints, emotion timeline and optional embedding index │
└──────────────┬───────────────────────────┬───────────────┘
               │                           │
               ▼                           ▼
┌────────────────────────┐  ┌────────────────────────────┐
│ Dashboard（可选）       │  │ Remember-Me（可选外部）     │
│ auth / search / import  │  │ privacy-cleaned assets      │
│ archive / network       │  │ metadata / search / viewer  │
└────────────────────────┘  └────────────────────────────┘

        provider / embedding / backup / diagnostics
             都是显式配置的附加边界，不是 core 前提
~~~

## 可选、实验性与 Operator-only 能力

| 能力 | 状态 | 边界 |
| --- | --- | --- |
| Embedding、semantic recall、related linking | 可选 | 需要 provider 和本地 index；不可用时 core 回退 lexical/fuzzy。 |
| Dehydration、长内容分析、冲突检测、digest | 可选/有成本 | provider-backed；失败会显式报告，不应把 fallback 当成完整分析。 |
| Dashboard、HTTP、导入 | 可选 | 需要有意部署和认证边界；不属于 Minimal Mode。 |
| tg profile 与 tg_summary | 实验性 channel capability | 只是输出 profile；不包含 Telegram transport 或 bridge。 |
| Remember-Me | 可选外部集成 | 单独安装、单独数据根和单独兼容边界。 |
| OMBRE_DIAG_TOOLS | 开发/诊断 | 默认关闭；用于 asset transport、ingest、图像导出和视觉验证 probes。 |
| Raw Evidence | Internal / Operator-only | 默认关闭，不进入 Breath、普通 recall、embedding、boot、Dream 或 model context。 |
| GitHub/OIDC backup、backup-v2 | Operator-only | authority、OIDC、密钥、冻结窗口和存储根必须由 operator 明确配置。 |

### Raw Evidence 的公开边界

Raw Evidence 源码随 Abide 开源，但它不是普通用户功能。它默认关闭，当前没有普通 Dashboard 浏览器、普通 MCP tool、Breath/recall、embedding、boot、Dream 或 model-context surface。它与普通 memory bucket、Remember-Me asset authority 和 portable export 分开；不要在普通安装或 Quick Start 中配置它，也不要将 preserve_raw 理解为 Raw Evidence。

### Diagnostics

设置 OMBRE_DIAG_TOOLS 后，当前额外暴露 15 个诊断工具，覆盖 attachment/asset ingest、browser upload、图像导出和 vision challenge 等开发/验收路径。它们用于检查 transport 和 integration，不代表稳定的普通用户 workflow；默认配置关闭。

## 安全边界

- **本地优先**：默认 transport 是 stdio；最短路径不启动 HTTP，也不要求 provider key。
- **数据目录隔离**：推荐把 OMBRE_BUCKETS_DIR 放到 checkout 外；不要把 bucket、SQLite、archive、uploads、cache、logs、exports 或 generated asset data 提交到 Git。
- **封存默认不可见**：sealed 内容不进入普通 recall、统计、导航或 embedding；显式 include 路径也必须符合当前授权边界。
- **来源不升级**：summary、inference、system 和 unknown 不能冒充原话；said_by、said_at、source_bucket 不根据猜测填写。
- **破坏性操作可预览**：delete、digest 执行和 unpin 等高影响操作需要当前计划绑定的确认；token 短时、一次性、不可跨计划复用。
- **历史先写快照**：正文破坏性变化和 delete 前，先写 write-ahead history；但 MCP 不提供普通 undo。
- **HTTP 显式认证**：无 OMBRE_AUTH_TOKEN 时 HTTP MCP 默认不可用，除非明确启用受控 anonymous opt-in；query token 是额外兼容路径，不应默认启用。CORS 也应显式配置。
- **Dashboard 认证**：Dashboard 的 setup/login、session、写入和 CSRF 边界与 MCP stdio 是两套入口；不要因为页面能启动就把它公开到网络。
- **凭据不入库**：API key、MCP token、Dashboard password、hook token、response seal、provider endpoint 和 backup authority 留在本地环境/配置，不写 README、测试或日志。

完整安全说明见 [SECURITY.md](SECURITY.md)。

## 当前限制

- 当前 clean-install 只验证了 WSL、Python 3.12.14、新建 virtualenv、仅安装 requirements.txt、无 .env、无 API key、无 Remember-Me、外置空数据目录、stdio MCP、一次 hold/breath 和重启后持久化。不要据此声称 Windows、macOS 或所有 Python/客户端都已 clean-install 验证。
- HTTP Full Mode 不是最短路径，也不是当前 clean-install-ready 发行模式：当前 bind 是 0.0.0.0，Uvicorn 不是 core 的 direct dependency。
- Remember-Me 的 ChatGPT 兼容性目前不支持且原因未诊断；其他非 Claude MCP/LLM client 仍未验证。这个限制不适用于 Abide core MCP。
- as_of 只能读取仍存在、当前可见 bucket 的历史正文；不提供完整历史 metadata、deleted bucket reconstruction 或 historical semantic search。
- code profile 目前没有 tg 那样完整的逐项截断透明度；不要把机械预算截断解释成完整上下文接续。
- tg 是 profile，不是 Telegram bridge；长相守不包含私人消息传输实现。
- 普通 portable export 不含 sealed/RM external authority/Raw Evidence/secret/config 等完整恢复材料；它不是 disaster recovery。
- 没有 suppression / include_suppressed 这一额外状态。当前可见性主要由 dormant（不主动浮现）、superseded（降权）和 sealed（隐藏）三层组成。
- 仓库没有把完整 MCP JSON contract 作为单独静态 README 复制品；schema 由当前 MCP 注册生成，参数精度应以运行时 discovery 和操作手册为准。

## License、来源与致谢

- Abide 自有及修改后的 Covered Code：[CPAL-1.0](LICENSE)
- 上游 MIT attribution 与 notice：[NOTICE.md](NOTICE.md)
- 第三方代码与运行时说明：[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- 设计与研究致谢：[ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md)

公开身份是 **Ting (peanutsuee)**。Abide 的代码 lineage 包含并衍生自 [P0luz/Ombre-Brain](https://github.com/P0luz/Ombre-Brain) 的适用部分；这些部分保留其 MIT copyright 和 permission notice。Haven-Ombre 只属于 design provenance，不是 Abide 的代码来源。

## 安装与快速开始

安装放在功能说明之后，方便先理解哪些组件需要什么边界。最短、当前已验证的路径是 WSL + Python 3.12.14 + stdio MCP。

### Minimal Mode

Minimal Mode 的强制环境变量数量是 **0**。仍然强烈建议显式设置 OMBRE_BUCKETS_DIR 到 checkout 外的可写目录，以免运行时 fallback 在源码目录创建 ./buckets。

~~~bash
git clone https://github.com/peanutsuee/Abide.git
cd Abide

python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

export OMBRE_BUCKETS_DIR=/path/to/abide-data
export OMBRE_TRANSPORT=stdio

.venv/bin/python server.py
~~~

Windows 等价的 virtualenv 命令可以使用 .venv\Scripts\python.exe，但当前 clean-install smoke validation 不覆盖 Windows；请先阅读 [docs/INSTALLATION.md](docs/INSTALLATION.md)。

Minimal Mode 不需要 .env、API key、Dashboard、Remember-Me 或 embedding provider。第一次写入可能初始化 history、asset、dehydration、embedding 等本地支持结构，但这不表示对应可选功能已经启用。

## 配置

从 [.env.example](.env.example) 选择需要的配置，不要把示例中的占位符当成默认必填项：

| 目标 | 主要配置 | 说明 |
| --- | --- | --- |
| 数据目录 | OMBRE_BUCKETS_DIR | 推荐设置到 checkout 外；core 最值得显式配置。 |
| Transport | OMBRE_TRANSPORT=stdio | 默认就是 stdio；HTTP 使用 streamable-http 属于高级路径。 |
| 写入分析/dehydration | OMBRE_API_KEY、OMBRE_BASE_URL、OMBRE_DEHYDRATION_MODEL | 仅在需要 provider-backed 分析、长内容处理或相关维护时配置。 |
| 语义 embedding | OMBRE_EMBEDDING_API_KEY、OMBRE_EMBEDDING_BASE_URL、OMBRE_EMBEDDING_MODEL | 与写入分析可独立配置。 |
| Remember-Me | OMBRE_RM_RUNTIME_ENABLED、OMBRE_RM_DATA_ROOT、OMBRE_PUBLIC_BASE_URL | 先安装可选依赖，并使用独立数据根。 |
| Dashboard | OMBRE_DASHBOARD_PASSWORD 或 setup token | 仅在有意部署 Dashboard 时配置。 |
| HTTP | OMBRE_AUTH_TOKEN、OMBRE_HTTP_ALLOWED_ORIGINS 等 | 不用于 Minimal Quick Start；必须同时审查认证、CORS 和 bind。 |
| conflict detection | OMBRE_CONFLICT_DETECTION_ENABLED | 可选 provider-backed warning，不是自动事实裁判。 |
| diagnostics | OMBRE_DIAG_TOOLS=false | 默认关闭；开发/验收才打开。 |
| backup | OMBRE_BACKUP_REPOSITORY 或 backup-v2 专用配置 | Operator-only；缺失 authority 时 fail closed。 |

config.example.yaml 仍可帮助理解较完整的运行参数，但环境变量和当前实现/guide 才是实际边界。不要把 provider、backup、HTTP 或 raw evidence 配置复制进 Minimal Mode。

## 接入 MCP 客户端

不同客户端的配置键名可能不同，下面只展示启动形状：

~~~json
{
  "mcpServers": {
    "abide": {
      "command": "/path/to/Abide/.venv/bin/python",
      "args": ["/path/to/Abide/server.py"],
      "env": {
        "OMBRE_TRANSPORT": "stdio",
        "OMBRE_BUCKETS_DIR": "/path/to/abide-data"
      }
    }
  }
}
~~~

stdio 进程通过 stdin/stdout 说 MCP，不是交互式终端 UI。客户端启动时会 discovery 当前 tool schema；因此不要把 README 中的工具数量或表格当作替代 schema。

## 验证安装

使用 MCP 客户端写入一条合成测试记忆，然后使用 full recall 读取：

~~~text
hold(
  content="Abide first-run test memory",
  tags="project/abide",
  importance=5
)

breath(
  query="first-run test memory",
  touch=False,
  mode="full"
)
~~~

预期结果是第二次调用能读到刚写入的 bucket；重启服务后再次读取，仍能在指定数据目录中找到它。验证完成后删除合成内容时，仍应遵守 trace(delete=True) 的 preview → confirmation 流程。

不要用真实私密对话做第一次安装验证，也不要为了验证 Minimal Mode 提前配置 provider、HTTP、Remember-Me 或 backup。

## 进一步文档

- [Abide 操作手册](docs/ABIDE_GUIDE.md)：当前工具行为、参数边界、sealed、provenance、supersession、维护和已知限制。
- [安装与首次运行](docs/INSTALLATION.md)：Minimal Mode、可选组件和 HTTP 限制。
- [Remember-Me integration](docs/remember-me-integration.md)：外部依赖、启用边界和兼容性状态。
- [安全政策](SECURITY.md)：数据目录、HTTP/Dashboard、密钥、Raw Evidence 和 backup authority 的安全边界。
- [环境变量示例](.env.example)：仅列出可选配置占位符，不含真实 secret。
- [License / Notice / Third-party / Acknowledgements](LICENSE)：完整法律与来源文件见 [NOTICE.md](NOTICE.md)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md)。
