# 湖裁验台设计说明

## 数据流

```text
configs/demo.yaml + queries.yaml
        │
        ▼
config.py ──► manifest.py ──► partition.py
        │          │                │
        │          ▼                ▼
        │      stats_audit.py ──► predicate.py / predicate_ast.py
        │          │                │
        │          └──────┬─────────┘
        │                 ▼
        │             prune.py ──► risks.py
        │                 │            │
        └────────────► runner.py ◄─────┘
                          │
          ┌───────────────┼────────────────┐
          ▼               ▼                ▼
    report_json.py  report_markdown.py  report_html.py
                          │
                          ▼
                     storage.py (SQLite)
                          │
                          ▼
                     server.py (只读回环查看器)
```

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `cli.py` | 解析子命令，组织 demo / init-demo / validate-stats / audit / report / golden / serve，映射稳定退出码 |
| `config.py` | 加载 YAML 配置与查询，解析相对路径，校验表、列、查询 ID 和 `fail_on` |
| `models.py` | 领域对象（ColumnStats、FileEntry、TableManifest、Risk、StatsCheck、AuditResult）及错误类型 |
| `manifest.py` | 读取并校验 `_manifest.json`，解析分区、路径安全、统计、空分区与 manifest 指纹 |
| `partition.py` | 从文件路径解析多级分区，校验类型/缺列/重复键/声明不一致，执行精确分区三值裁剪 |
| `predicate_lexer.py` | 无第三方依赖的 tokenizer，供自研 parser 使用 |
| `predicate.py` | 递归下降 parser + 类型校验，生成显式 AST，不使用 eval/exec |
| `predicate_ast.py` | AST 节点、稳定渲染、三值逻辑组合、叶节点遍历和 AST JSON 序列化 |
| `stats.py` | 校验声明统计，基于 min/max/null_count 做保守文件级决定；分区列使用精确分区值 |
| `stats_audit.py` | 重新读取 CSV/JSONL，重算统计，校验类型、大小、哈希、行数，输出可信度与 untrusted map |
| `prune.py` | 组合分区阶段与统计阶段，构建可解释的 QueryPlan/FilePlan 和误裁剪候选 |
| `risks.py` | 八类风险的生成、稳定去重排序、级别统计和退出码 gate |
| `runner.py` | 端到端编排：加载、复核、裁剪、风险、报告、SQLite 持久化 |
| `storage.py` | SQLite schema、运行记录、查询计划/裁剪结果/风险/统计检查的读写 |
| `report_json.py` | 规范 JSON 渲染、深度差异和 golden 比较 |
| `report_markdown.py` | 确定性 Markdown 报告 |
| `report_html.py` | 自包含、无外部资源的 HTML 报告 |
| `server.py` | 仅回环监听的只读 HTTP 查看器，`/healthz`、运行列表与详情，拒绝写方法 |
| `demo_data.py` | 从 `demo_spec.json` 确定性生成合成 fixture、查询和配置 |
| `utils.py` | 规范化 JSON、sha256、类型 coercion、日期校验、路径安全、稳定 ID 和级别排序 |

## 裁剪流水线

1. **加载 manifest**：校验表名、格式、schema、分区声明、文件路径安全、sha256 格式和逐文件统计。路径解析出的分区值必须与 manifest 声明一致。
2. **解析查询**：每个查询按表 schema 解析为 AST；错误转换为带查询 ID 的 `QuerySpecError`，保留原始稳定错误码。
3. **复核统计**：对每个非分区列重新读取数据、重算 min/max/null_count，并检查文件大小、sha256、行数、声明类型和边界。任何异常的列进入 `untrusted`；文件完整性失败则整文件列不可信。
4. **分区裁剪**：按分区标签分组，用精确分区赋值求值；非分区列叶子在分区阶段返回 unknown，不误裁。AND/OR/NOT 使用 `{True, False, None}` 三值组合。
5. **文件裁剪**：对未分区裁剪的文件使用声明统计求值；不可信、缺失或无效统计返回 unknown。分区列始终使用精确分区值，不用文件统计。每个驱动裁剪的谓词叶子还会计算边界余量：以该类型的记录分辨率为单位（float 取 canonical 12 位有效数字量子、int/bool 取 1、date 取 1 天、datetime 取 1 秒、string 取首个不同位置的码点距离），余量 0 表示精确边界决策（依赖运算符严格性，不收紧），小于 1 个单位表示余量不足。余量不足的 `must_not_match` 自动收紧为 `maybe_match`（只增加扫描候选，绝不减少；`must_match` 永不降级），FilePlan 保留收紧前后决定、逐叶子余量数值与原因证据。
6. **误裁剪证据**：若某文件因不可信统计被保留，但盲信声明统计会得到 `must_not_match`，则标记 `would_declared_prune` 并生成 `MIS_PRUNE_RISK`。
7. **报告与台账**：报告只包含确定性内容（无 run ID、无时间戳）；运行时间和 run ID 仅进入 SQLite。JSON/Markdown 字节稳定，HTML 结构稳定。

## 确定性与排序

所有集合在进入报告前排序：表按名称，查询按 `query_id`，文件按 manifest 相对路径，风险按级别/码/查询/表/文件/描述/ID，统计检查按表/文件/列。JSON 使用 `sort_keys=True`、固定 separators 和尾随换行；Markdown/HTML 由同一有序数据生成。
