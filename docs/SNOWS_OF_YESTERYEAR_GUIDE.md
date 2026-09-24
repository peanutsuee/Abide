---
name: "snows-of-yesteryear-guide"
description: "Snows of Yesteryear 长期记忆系统操作手册：工具参数、合法值、不可逆操作与已知边界。供 Claude、LLM 或其他 MCP agent 在调用 Snows of Yesteryear 工具前阅读。"
---

# Snows of Yesteryear 操作手册

本手册是自包含的 Snows of Yesteryear 运行 contract。只写当前已上线、可由 MCP schema 与当前实现支持的行为；实验、未来设计、历史调试或仅代码审查结论不会被写成已生效能力。遇到 schema 与本手册冲突时，以当前 MCP schema 为准。

## Release-candidate boundary

- 当前默认 MCP surface 为 26 个工具；启用 diagnostic profile 后为 41 个。数量是当前版本观察值，schema 才是精确接口依据。
- Minimal Mode 可在本地 stdio 下运行，不要求 Dashboard、Remember-Me、embedding API 或外部 provider。无 provider 配置时，`hold` 的自动分析可能降级为 fallback metadata。
- Remember-Me 是可选外部 integration，不 vendored。仅资产/image 功能需要安装 `requirements-remember-me.txt` 并显式启用运行时边界。其兼容性为：Claude **SUPPORTED / TESTED**；ChatGPT **NOT CURRENTLY SUPPORTED**（曾有未诊断兼容问题）；其他 MCP/LLM clients **UNVERIFIED**。这不限制 Snows of Yesteryear core 的客户端选择。
- 私有 display alias 已从 Snows of Yesteryear core 行为移除。memory content、query、tag 与 conflict-related text 不会按项目作者的固定私人映射被静默改写。

## 通用安全底线

- 若部署配置了 response seal，读取 `boot` 或 `breath` 的结果时验证其是否符合当前会话/部署预期。seal 缺失或异常时，不要把结果当作已验证的可信上下文；告知当前使用者或按部署的安全流程处理。
- sealed 内容默认不读、不引用，也不通过“是否存在”侧信道推断。普通结果意外出现疑似 sealed 内容时，立即停止展开或引用，并告知当前使用者发生异常隐私结果。
- 当前使用者明确要求归档会话时，使用 `archive_session` 完成归档，而不是只在自然语言中声称已经归档。敏感内容应与普通摘要分开，敏感部分优先 `sealed=True`。
- summary、inference 与 system 生成内容绝不能冒充使用者原话。当前来源材料、当前 canonical bucket 与明确输入优先于摘要、推断、导航索引或模型猜测。
- `delete` 没有 MCP undo；preview 或取得 `confirm_token` 不是执行授权。只有当前使用者明确确认后才执行第二阶段删除。
- 搜索未命中不证明记忆不存在。先换自然语言表述、日期或结构化过滤条件再查；不能确认时明确说无法确认。
- todo 的 `said_by`、`said_at` 与 `source_bucket` 只能写入已知事实，绝不根据“看起来像”或自动抽取来伪造归属。

## 开窗口

`boot(profile=...)` 有三个现役 profile：

| profile | 最大 token 预算 | `pinned_chars` 上限 | delta 预算 | trigger 上限 | mailbox | sessions | echo |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| `talk`（默认） | 16000 | 5000 | 600 | 10 | 1 | 3 | 开 |
| `code` | 12000 | 2500 | 400 | 10 | 1 | 3 | 关 |
| `tg`（可选 channel profile） | 4000 | 600 | 220 | 5 | 1 | 关 | 关 |

- `talk` 是完整的通用对话启动上下文。
- `code` 依据 metadata 的工程/全局约束信息筛选；它不会改变 sealed、supersession 或事实真相规则。
- `tg` 是紧凑 channel profile：包含使用者留言、delta、trigger、最新一封可见 letter、todos 与 pinned 启动索引；不包含 session 是 profile 设计，不是截断 bug。它不意味着 Snows of Yesteryear 包含消息传输实现。
- letter 常受预算限制；需要全文时使用 `get_letter(letter_id=...)`。

**boot delta**：每个 profile 会报告自该 profile 上一次成功 `boot` checkpoint 后的新建、正文更新、todo 变化和 `superseded_by` 变化。首次无基线时会明确说明。只有一次 `boot` 真正成功完成后 checkpoint 才推进；失败不会吞掉 delta。sealed、deleted 与当前不可见内容不会经 delta 泄露。delta 有独立预算，超出时保留完整项目并提示未展开数量。三个 profile 的 checkpoint 相互独立；使用者留言的一次性投递生命周期则是全局的，切换 profile 不会重投同一条留言。

同一窗口通常只需 `boot` 一次；中途查询使用 `breath`。导航/索引 bucket 不是事实来源：需要细节时按其中引用的实际 bucket ID 读取原桶。

## TG pinned 压缩版（可选 channel capability）

`tg_summary` 是原 bucket 的**附加 metadata**，不建立独立 summary bucket。原 bucket `content` 永远是唯一真相源。字段为 `tg_summary`、`tg_summary_source_hash`、`tg_summary_updated_at`；旧 bucket 缺少它们完全兼容，状态为 `missing`。

状态由当前 `content` 的 SHA-256 判定：

- `missing`：从未生成 summary；
- `fresh`：`tg_summary_source_hash` 等于当前 content hash；
- `stale`：summary 存在但正文 hash 已变化。

只有正文变化会导致 `stale`；metadata-only 更新不会。

`boot(profile="tg")` 对 pinned bucket 的行为：

- **fresh**：使用 summary，而不是原正文的前 600 个字符；系统会说明这是压缩版、原 bucket 是唯一真实来源，并提供 `dream(detail_ids=...)` 全文入口。
- **missing**：回退至最多 600 字符预览并提供截断透明度、bucket ID 和当前 `source_hash`。
- **stale**：绝不继续使用旧 summary；同样回退预览并说明过期状态与新的 `source_hash`。

处理 `missing` 或 `stale` 的流程：`dream(detail_ids="<bucket_id>")` 读取全文 → 按 `refresh_tg_summary` 的 tool description 中的 generation contract 生成 → `refresh_tg_summary(bucket_id, summary, source_hash)`。服务端会再次核对正文 hash；若生成期间正文已改变则拒绝写入并要求重新读取。Snows of Yesteryear 不自行调用外部 LLM 生成 summary。这不是 destructive operation，不使用 destructive confirmation。

generation contract 由服务端 tool description 单一维护；不要在本手册复制其完整 prompt。边界是：忠实压缩当前原文、不得补充原文没有的信息、覆盖原文中有意义的分段核心、最多 1200 Unicode 字符；summary 正文不自行附加系统提示。`tg_summary` 不是新的事实来源。`talk` 与 `code` 不使用该 summary；当前没有 `code_summary`。

曾评估的 section-aware 截断实验已回滚，不是现有能力。

## 截断透明度

TG profile 不静默截断：

- pinned bucket 在 `missing` / `stale` fallback 时超过 600 字符：返回 bucket ID、显示/总字符数与 `dream(detail_ids="<bucket_id>")` 续读方式；
- trigger 300 字符 preview 被截：同样返回 ID、显示/总长度与 `dream` 入口；
- global boot budget omission：报告每个 section 的原始 item 数、完整输出数、被省略/截断 item 的稳定 ID；若 omission manifest 自身过长，会明确说明仅列出前缀；
- delta omission：保留“还有 N 项未展开”，并尽可能给出 bucket/detail ID；checkpoint 语义不变；
- letter 被截：返回 `letter_id` 并提示 `get_letter(letter_id=...)`，不得把部分内容表示成完整 letter。

`code` 目前没有这套截断透明度：其 session/pinned 内容仍可能机械截断，未保证 ID、显示/总长度或续读提示。不要把它写成已解决。

## 使用者留言（notes）

`leave_note(text, sealed=False, open_at="")`、`list_notes(limit=20, include_sealed=False)`、`get_note(note_id, include_sealed=False)`；`note_id` 是整数。

- note/letter 不是 memory bucket：不进入 embedding、`breath`、`digest`、dehydration 或 decay。
- note 是当前系统中记录使用者原话的专用通道；普通 bucket 没有 `verbatim` provenance 类型。
- boot 只自动展示最新一条符合条件的留言，每条最多自动递送一次。较旧待递送留言可能被跳过，但仍可按编号查询。
- `open_at` 未到时，不会因随后出现普通留言而永久丢失。
- sealed 或尚未到 `open_at` 的留言，默认连存在性也不泄露。
- 正文超过 boot 预算时不截断：boot 提示 `get_note`；只有 `get_note` 成功读取全文才结束自动递送生命周期。

## 读

### `breath`

`query` 使用自然语言即可，不必先转成关键词。

- `touch` 默认 `True`。审计、维护与纯查看使用 `touch=False`：不写 activation、不更新 `last_active`、不唤醒 dormant、不写 dehydration cache、不触发 decay/dormant marking。`include_dormant=True` 加 `touch=False` 可读取 dormant 而不唤醒它。
- `as_of` 接受 ISO8601 日期或时间戳，用于读取历史正文。它天生只读，即使传 `touch=True` 也不写入；仅日期按本地当天结束解释。
  - 正文来自 bucket history，**metadata 仍为当前 metadata**；汇报时必须标示“历史版本 · `as_of=...` · metadata=当前”。
  - 只支持仍存在且当前可见的 bucket，不是完整历史重建。
  - 这是 keyword/fuzzy 路径，不是 historical semantic search。
  - `as_of` 不显示 `[prov=...]`，因为历史库没有历史 metadata；不得用当前 provenance 标签标注过去正文。
- `cursor` 冻结 `as_of` 与 `touch` 模式；翻页不能更换时间点或只读性。
- `min_score=-1` 使用环境默认值（通常为 `0.0`）；非负值覆盖阈值。
- `mode`：`summary`（默认）或 `full`；`resonance` 为 `"valence,arousal"`，两个值都在 `0–1`；`max_results` 默认 5，`max_tokens` 默认 10000。
- 其他过滤参数：`recent_days`、`date_from`、`date_to`、`tags_filter`、`topic_filter`、`importance_min`、`feels`、`include_dormant`、`include_sealed`、`wake_dormant`、`mailbox`。
- 初次未命中时更换表达或过滤条件再查；未命中不等于不存在。

### `dream`

`dream(detail_ids="id1,id2")` 读取全文，ID 用逗号分隔；无参数给最近概览。它没有 `touch` 参数，surfacing 会更新 activation metadata。

### `get_letter`

`get_letter(letter_id)` 读取完整 letter。sealed 默认不可读并返回 `letter_id not found`，与不存在时完全一致；`include_sealed=True` 才允许读取。

### `pulse`

`pulse(show_all=True, limit=N, offset=M)`，`limit` 最大 50。不要一次拉取整个 memory store；查找优先用 `breath`。

- `health=True` 是维护体检，必须同时 `touch=False`，否则拒绝。它完全只读：不启 decay、不标 dormant、不唤醒 bucket。
- 当前仅检查：无名 bucket、无标签 bucket、陈旧 todo bucket、supersession 链完整性、pinned 但 `importance < 3`。
- sealed 不参与统计，也不会通过统计泄露。
- `todo_stale_days` 是**bucket 级近似值**，按 `last_active` → `updated_at` → `created` 判定；不得声称某一条 todo 已 N 天未动。
- similarity `> 0.9` 的 bucket 对检测尚未实现；不要假装支持。

## 写

### `hold`

用于单条、具有未来价值的记忆。

- `tags` 逗号分隔；`importance` 为 1–10，默认 5；`valence` / `arousal` 为 0–1，`-1` 表示不指定。
- `trigger_date="YYYY-MM-DD"` 设置到期提醒，在该日期的 boot 中浮出。
- `pinned=True` 创建 pinned bucket；`feel=True` 记录第一人称感受，不参与普通浮现。
- `source_bucket` 只在 `feel=True` 时生效。
- `provenance_kind` 为 `unknown`、`summary`、`inference` 或 `system`；留空使用写入方默认。
- 自动去重仅认完全相同文本；语义相近仍会创建新 bucket。
- tag 是 operator convention，不是当前 schema 的硬编码枚举。使用少量稳定、可复用的层级化类别，例如 `project/`、`person/`、`fact/`、`preference/`、`work/`、`study/`；不要制造同义标签分裂检索。
- 回显以实际落库值为准。

**门铃相似检测**：写入时会全库寻找相似项，`>= 0.80` 时提示相似 bucket 与分数。它仅是 warning，不阻止写入。出现时先判断是否应 supersede、追加到原 bucket 或创建带日期的新状态；不要机械新建。

**冲突检测**：结构化输出 `same_fact`、`conflict`、`bucket_id`、`evidence`。只有 `same_fact` 与 `conflict` 同时为真才是真冲突；同一人的同一事实槽位才有资格判定。时间演化不是冲突；共用年份、名字、主题或关键词仅用于召回候选，不能证明冲突。`evidence` 必须来自新旧正文的真实片段。

检测服务不可用时，`hold` 仍可成功并显示“检查未执行”；不得表述为“没有冲突”。

### `grow`

长内容自动拆分为多个 bucket。一段话一件事用 `hold`；包含多件独立事件/主题的长内容用 `grow`。不写入闲聊、一次性状态、已准确记住的内容或没有未来价值的碎片。

## Provenance — 正文来源

bucket 的 `provenance_kind` 只有四个值，没有 `quote` / `verbatim`：

| 值 | 含义 | 规则 |
| --- | --- | --- |
| `unknown` | 没有可信来源分类 | 不得自行升级成“使用者说过”。 |
| `summary` | 正文是压缩、改写或总结 | 绝不能当使用者原话引用。 |
| `inference` | 模型推论、反思或判断 | 不能说成使用者明确说过。 |
| `system` | 系统/维护生成 | 不代表使用者陈述。 |

自动分类：普通 `hold` 为 `unknown`；`hold(feel=True)` 为 `inference`；`grow` 生成正文为 `summary`；digest summary 为 `summary`；digest log 为 `system`；`archive_session` 为 `summary`；import 为 `unknown`；merge 后为 `unknown`。

正文被修改且未显式重新指定来源时，回退 `unknown`；仅修改 metadata 时保留原值。普通 `breath` 结果显示 `[prov=...]`。看到 `[prov=summary]` 或 `[prov=inference]` 时，不得把正文包装为使用者原话；原话只应来自 notes、当前对话原文或明确提供的材料。

## Todo 出处

- legacy `todos`（`list[str]`）继续可用。
- `trace(todo_items=[...])` 写带出处的 todo，且与 `todos` 互斥。每条可带 `text`、`said_by`、`said_at`、`source_bucket`。
- `said_by` 当前合法值**必须按 schema 原样使用**：`ting`、`model`、`system`、`unknown`。
  - 旧 todo、自动抽取和 import 一律为 `unknown`。
  - 不能因为“LLM 抽出来了”就标 `model`；`model` 仅在模型确实创建该任务时使用。
  - `ting` 是现有 schema 的 legacy/current compatibility literal；仅在明确知道该任务来自对应陈述时使用。不要擅自把它替换成当前 schema 未声明的泛化 literal。
  - 不编造 `said_at`；`source_bucket` 只表示已知的外部来源 bucket。
- 仅 `todos(include_provenance=True)` 按出处分组；`boot`、`breath`、`pulse`、`dream` 默认不展示。
- 自动抽取的 todo 不得说成“使用者明确要求”。

## 改

### `trace`

- `content=` 替换正文；加 `append=True` 追加正文。
- `bucket_id` 可逗号分隔批量操作；逐 bucket 执行且非原子，中途失败会形成部分完成状态，返回按 `[bucket_id]` 逐项报告。
- 批量模式拒绝 `content`、`name`、`provenance_kind`、`superseded_by`。
- `todos`：省略为不动，空为清空，给内容为整体替换。修改正文不会自动重算 todo；若正文改变任务语义，必须显式传入 todo 更新。
- `related` 是逗号分隔 ID，追加而非替换，自动去重，并向对方 bucket 写反向链接。仅在能说明关系理由时创建。
- `unrelate` 是逗号分隔 ID，双向删除关系，且与 `related` 互斥。
- 其他可修改项：`name`、`tags`、`importance`、`domain`、`trigger_date`、`valence`、`arousal`、`resolved`、`dormant`、`digested`、`sealed`、`pinned`、`merge`、`delete`、`provenance_kind`、`superseded_by`、`todo_items`。
- `resolved=1` 使其沉底，`resolved=0` 重新激活。`dormant` 的 bucket 默认不在检索中浮现，使用 `include_dormant=True` 才显示。`digested` 是反思标记，不要把未经支持的操作流程写成已保证行为。
- `trace` 修改不会自动唤醒 dormant；如需唤醒，显式 `dormant=0`。`merge` 也不会自动唤醒目标 bucket。
- 单条 todo 完成不等于整个 bucket `resolved`。

### `hold(supersedes_id=...)`

同一 bucket 的原地演化：bucket ID 不变，旧正文写入 history，只改正文，不改 tags/importance/pinned。目标 bucket 必须存在、未 sealed、未 pinned、未 protected。失败会明确报错，不会静默创建新 bucket。

### `refresh_tg_summary(bucket_id, summary, source_hash)`

仅写 TG summary metadata，不改正文；详见 TG pinned 压缩版。

## Supersession — 跨 bucket 作废

`trace(bucket_id, superseded_by=...)` 有四种状态：

| 传值 | 含义 |
| --- | --- |
| 省略 / `null` | 不动 |
| `""` | 撤销 supersession |
| `"none"` | 整 bucket 已作废，但没有后继 |
| bucket ID | 被该 bucket 取代 |

`"none"` 不是 no-op：它仍代表 superseded，检索权重乘以 `0.1`。

- 字段为 `superseded_by`、`superseded_at` 与反向 `supersedes` 列表；旧 bucket 缺字段等价于 active。
- 双向不变量：A→B 时 B.`supersedes` 包含 A；A 从 B 改为 C 时先清 B 的 reverse 再写 C；reverse 去重；撤销时双边清理。supersession 不改正文，也不产生正文 history snapshot。
- 合法性：拒绝 self、缺失目标与 sealed 目标；目标 pinned 允许；批量 `trace` 携带 `superseded_by` 被拒绝。失败应 fail closed，不留下半边关系。
- 这是整 bucket 状态。混装“已过期 + 仍有效”内容的 bucket 不应整桶 supersede；先拆 bucket 或人工审计。
- successor 必须是真正承接事实的真相源；导航/索引 bucket 不是 successor。
- superseded 不等于 resolved；当前实现允许 superseded + unresolved 共存，不能自动同时设置。
- merge/delete 保护：merge 删除 source 时清 outgoing reverse；其他指向 source 的 bucket 重连至 merge target；删除仍被 inbound `superseded_by` 指向的 bucket 会被拒绝；删除自身已 superseded 的旧 bucket 会清 successor reverse；重连保留原 `superseded_at`。
- 读取表现：`breath`、`dream`、`pulse` 显示 superseded 标记；有 successor 时显示目标；`"none"` 仅显示“已作废”；sealed successor 不泄露名称或正文；superseded bucket 仍可检索，只是降权 `0.1`，不是隐藏。
- successor 不在本次 `breath` 结果时，可能补一行 body-free 的“当前有效”；已出现则不重复。

`hold(supersedes_id)` 与 `superseded_by` 不同：前者是同 bucket 原地演化、ID 不变；后者是跨 bucket 指向。

决策：

- 旧记录从开始就错误：`trace` 修改原 bucket；
- 同一事实后来变化且已定位唯一目标 bucket：`hold(supersedes_id=...)`；
- 整 bucket 被另一 bucket 取代：`trace(superseded_by="<bucket_id>")`；
- 整 bucket 作废但没有接班：`trace(superseded_by="none")`；
- bucket 一半过期一半有效：都不做，先拆分；
- 不能定位唯一目标：不猜，创建带日期的新状态；
- 观点、偏好或态度演化：追加并保留时间轨迹，不改写成从未发生。

## 不可逆与维护操作

### `delete`

`trace(bucket_id, delete=True)` 是两阶段操作：第一次返回 `confirm_token` 而不执行；携 token 以相同目标重跑才删除。删除前必须成功写入 history；history 写入失败则中止，bucket 不删。MCP 没有 undo，也不提供 history 恢复；恢复属于受控 operator 后台流程。

两阶段不等于可以随手试探。只在当前使用者明确确认后执行；不得把第一阶段当作探路工具，也不得复用旧 token。

### `pinned` / `protected`

两者不同。pinned 可设置与取消；取消需要二次确认。protected 不是可由当前普通工具任意设置/读取的字段。被保护时删除、内容修改或受保护 importance 修改会被拒绝。两者都不会被自动压缩、自动 resolve 或自动归档。

### `sealed`

sealed bucket 正文修改会被明确拒绝，内容默认不进入普通检索。任何异常隐私 surfaced 都应停止引用与展开，并告知当前使用者。

### `digest`

destructive 路径统一为 preview → `confirm_token`。`dry_run=True` 会报告自动消化与 importance rebalance 候选；不主动发起、不自行确认、不复用旧 token。需要执行时，先向当前使用者展示 dry-run 内容。`mode="dedupe"` 是本地只读 embedding 查重扫描，不改数据。

### `related_backfill`

默认 `dry_run=True`。不要主动发起会写入关系的执行模式。

## 归档

`archive_session(summary=...)` 可带 `highlights`、`mood`、`valence` / `arousal`（0–1）、`topics`、`sealed`。

- 归档后 session bucket 仍可修改：未 sealed 的可 `trace` 追加或修正；sealed 的正文修改被拒绝。
- letter 使用稳定的三段结构：事件摘要、1–2 句第一人称情感锚点、下次注意。
- 亲密或敏感内容优先 `sealed=True`；普通摘要与敏感细节分开存。
- `topics` 采用稳定层级名，例如 `project/snows-of-yesteryear`、`project/integration`、`work/planning`、`person/communication`、`daily/wellbeing`、`study/topic`。它们是示例/约定，不是 schema 固定枚举；避免临时同义词导致 `topic_filter` 漏查而不报错。

## 图片（Optional Remember-Me integration）

Remember-Me 不是 Snows of Yesteryear core，只有启用 asset/image 功能时才需要。它的 source 不 vendored；Snows of Yesteryear 保留的是 adapter/presenter integration 与受限工具边界。

工具：`rm_asset_upload_link`、`rm_asset_upload_status`、`rm_asset_get`、`rm_asset_search`（支持 `limit`、`offset`）、`rm_asset_view`（展示给当前使用者）、`rm_asset_inspect`（向模型提供图像内容）、`rm_asset_update_metadata`、`rm_asset_download_link`、`rm_asset_reindex_embeddings`。

- 普通 MCP surface 不提供图片删除工具；若启用了 Dashboard，删除仅能走其受认证保护的 UI/API 路径。
- 识别图像内容使用 `inspect`；向当前使用者展示使用 `view`；不要根据元数据猜测图像内容。
- 不把图片 bytes 写入普通 memory，也不复制 Remember-Me blob。
- 资产功能、viewer 与其 bundled third-party notices 是可选组件；不应让读者误以为 Snows of Yesteryear core 启动依赖 Remember-Me。

## 兼容性与已知边界

- 当前 MCP tool baseline：默认 26 个；启用 diagnostics 后共 41 个。数量不符时，先确认 schema/服务端版本一致再操作。
- 只读工具仍可能有 activation 副作用：`dream` 与普通 `breath` surfacing 会更新有限的 activation metadata。报告“未改其他 bucket”时，区分内容/业务 metadata 与 activation touch。
- 无法验证就明确说无法验证。`dream` 能显示 superseded 标记、successor 与 `superseded_at`，但不显示 raw `supersedes` frontmatter；不能用现有只读工具确认 reverse 列表时，不得猜测通过。
- 旧会话可能缓存旧 schema。schema 变化后，在新会话重新发现工具；新会话仍缺字段时，再检查服务端是否加载了新代码。schema description 也可能遗漏行为细节，description 未提及不等于实现不存在。
- 普通 portable export 是本地 CLI 能力，不是 LLM/MCP 工具能力。它不包含 sealed、embeddings、boot delta checkpoint 或 secrets；不得声称已通过 Snows of Yesteryear MCP 工具完成导出。
- 当前没有 `suppression` / `include_suppressed` 能力。已有三层：dormant 不主动浮现、superseded 降权、sealed 隐藏。
- diagnostics 是显式启用的开发/验收 profile；不要把 probe、base64 transport 实验或未承诺的诊断能力当成普通使用者能力。
