# 风险判定规则

风险由 `risks.py` 从三个来源汇总：manifest 解析问题、统计复核问题、查询裁剪计划。风险 ID 由风险码、级别、查询 ID、表、文件、描述和证据的规范 JSON 计算 sha256 前缀，保证同输入稳定去重。最终按级别（high → warn → info）、风险码、查询 ID、表、文件路径、描述和 ID 排序。

## 八类风险

### `MIS_PRUNE_RISK`（high）

对每个查询、每个仍为扫描候选的文件：如果该文件带有与谓词相关的不可信列，则用“盲信声明统计”重新求值一次。若声明统计会给出 `must_not_match`，说明不可信统计本来会导致文件被错误跳过，而此时实际决定是保留。证据包含：

- `declared_stats_decision`：盲信声明统计的决定；
- `effective_stats_decision`：降级后的实际决定（通常 `unknown`）；
- `untrusted_columns`：涉及的不可信列；
- `stats_reasons` 与 `statistics`：原始统计快照和谓词证据。

该风险直接对应“不可信统计不得静默用于裁剪”的要求。

### `FULL_SCAN_RISK`（warn）

出现以下任一情况时进入：

- 分区路径缺少声明分区键（manifest/partition 解析问题），产生风险并保留扫描；
- 某查询所有文件都无法被分区或统计裁剪，即 `candidate_files == total_files` 且文件数大于零。

证据包含总文件数、候选文件数、字节数和分区统计。谓词本身可下推但统计不可信也会导致全扫描，因此与 `MIS_PRUNE_RISK` 可同时出现。

### `MISSING_STATS`（warn）

文件 manifest 中缺少某非分区列的统计（`declared_stats` 为 `None`），或 schema 中该列没有对应文件统计。复核时标记 `missing_stats`，该列加入 untrusted；不用于裁剪。

### `STATS_MISMATCH`（high）

统计复核状态为 `stats_mismatch`、`read_error` 或 `invalid_stats`：

- 声明的 `type` / `min` / `max` / `null_count` 与重算结果不一致；
- 文件大小、sha256 或行数与 manifest 不一致（完整性失败，整文件列降级）；
- 声明统计本身无效（类型不支持、`min > max`、`null_count` 超过行数、非空列缺边界等）；
- 无法读取 CSV/JSONL。

证据包含列名、声明值、实测值或读取错误消息，以及复核消息。

### `TYPE_DRIFT`（high）

来自两类检查：

1. 分区路径或 manifest 分区值与声明类型不兼容，或分区路径与 manifest 分区值不一致；manifest 统计引用未知列也按此记录；
2. 统计复核发现实际数据值违反声明列类型（例如 int 列出现 `retry`）。证据包含前 3 个漂移样本和 `type_drift_count`。

此类列不用于裁剪，避免类型误判。

### TYPE_DRIFT 传导分析（`drift_propagation` 报告段）

`TYPE_DRIFT` 风险本身只标记“哪些文件哪些列漂移”。报告顶层的 `drift_propagation` 段进一步回答：某条漂移是否真的走进了某个查询对某个文件的裁剪求值路径、最终保留是否被它改变。漂移列来源为 manifest issue（含分区路径问题）与统计复核的 `type_drift` 状态。每个被引用漂移列的 （查询， 文件） 产生一条记录：

- `propagated_columns` / `non_propagated_columns`：逐列结论。未被谓词引用的漂移列记为 `not_referenced`；与分区列重名的漂移列由 `exact_leaf_evaluation` 精确求值覆盖漂移信号，记为 `partition_exact` 并附 `exact_leaves` 精确结果。两者均为不传导。
- `leaves`：每个被传导列命中的叶子片段（comparison / in / between / is_null），含有效求值（降级为 `[null, false, true]`）与盲信求值的真值集合、`branches`（AND/OR 祖先分支、左右侧、另一侧是否也受漂移影响）以及 `not_evidence`（叶子处于 NOT 之下时，记录该子树漂移降级前后的三值集合，即它对父级的贡献方向）。
- `asymmetric_nodes`：仅在 AND/OR 节点上、且文件最终真值仍含 True（未裁剪）时，标注“盲信为 `must_not_match`、降级后变为非 `must_not_match`”的不对称结论。
- `decisions` 与 `conclusion`：`effective`（实际降级求值）与 `trusted_without_drift`（盲信漂移列声明统计的反事实）两组决定；`pruning_trustworthy` 为假当且仅当盲信会裁剪而实际保留（`changed_by_drift`），此时 `retained_only_as_unknown` 表示该文件是在漂移列逐叶子标注后仅以 `unknown` 保留。

该分析不产生新风险、不改变风险排序与退出码；分区阶段已裁剪的文件不进入统计求值路径，因此不产生记录。

### `NULL_SEMANTICS`（warn）

查询包含 `IS NULL` / `IS NOT NULL`，且某文件的相关谓词涉及不可信列（通常因 `null_count` 缺失、无效或整文件完整性失败）。因为空值谓词无法可靠判定，文件保留为候选；风险证据包含谓词渲染文本、不可信列和统计决定。

### `BOUNDARY_RISK`（warn）

查询谓词中出现以下片段之一时提示边界歧义：

- 日期或日期时间比较 / BETWEEN；
- float 的 `=` 或 `!=` 比较；
- 含大写字母的字符串比较（大小写/排序语义可能因引擎而异）。

证据分别列出日期类片段和 float/大小写类片段。

### `EMPTY_PARTITION`（warn）

manifest 声明了某分区，但该分区没有文件，或所有文件 `row_count == 0`。证据含分区值、文件列表和 `row_count: 0`。空分区不产生数据，但会提示表布局与统计治理问题。

## 统计复核状态

`stats_audit.py` 使用以下状态：

| 状态 | 含义 | 是否进入 untrusted |
| --- | --- | --- |
| `ok` | 声明统计与重算一致 | 否 |
| `missing_stats` | 某列统计缺失 | 是（该列） |
| `stats_mismatch` | 声明值与实测不一致或文件完整性失败 | 是 |
| `invalid_stats` | 声明统计自身无效 | 是 |
| `type_drift` | 实际值违反声明类型 | 是 |
| `read_error` | 文件无法读取 | 是（整文件） |

分区列不参与文件统计裁剪，也不参与统计复核（其值是目录路径的一部分），因此不会因“数据文件缺少分区列”产生误报。

## 退出码 gate

`exit_code_for_level(level, risks)` 仅由风险列表和阈值决定：

- `none` → `0`；
- `info` → 任意 info/warn/high → `2`；
- `warn` → 任意 warn/high → `2`；
- `high` → 任意 high → `2`；
- 无风险或低于阈值 → `0`。

CLI 输入/配置/查询错误返回 `1`；`demo` 不管风险如何固定返回 `0`，CI 应使用 `audit --fail-on <level>`。
