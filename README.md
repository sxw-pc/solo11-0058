# 湖裁验台（lakeprune）

## 1. 一句话定位与边界

**是**：一个纯本地、完全离线、可复现的数据湖分区裁剪与文件列统计下推可信度审计台。它读取本地湖目录、`_manifest.json`、CSV / JSONL 文件和声明式 YAML 查询，输出可解释的裁剪计划、统计复核结果、风险分类、JSON / Markdown / HTML 报告、SQLite 台账和 golden 回归结论。

**不是**：不执行真实 SQL；不连接 PostgreSQL / MySQL / Hive / Spark / Trino / 对象存储；不解析 Parquet / ORC / Avro footer；不维护表、不合并小文件、不做快照过期、版本账本、时间旅行、迁移、血缘、抽样、脱敏、调度或权限系统；不提供公网服务、外部 API 调用或写入后台。

业务 fixture 为虚构的环境监测读数表 `readings`、设备事件表 `events` 和测站维表 `stations`，不包含真实业务数据、公司名、凭据或外部目标地址。

## 2. 为什么做统计下推可信度审计

分区裁剪和 min/max/null_count 下推能显著减少扫描量，但它们默认“manifest 是可信的”。一旦统计缺失、`min > max`、`null_count` 越界、声明类型与数据不一致，或统计与文件内容不匹配，规划器就可能跳过本应扫描的文件，产生静默误裁剪；反之，分区路径缺列或谓词无法下推会造成被忽视的全扫描。

湖裁验台把两件事放在同一条审计链上：

1. 按谓词计算分区级和文件级裁剪决定，并保留每个决定的谓词片段与文件证据；
2. 重新读取本地 CSV/JSONL 复核 manifest 统计，把不可信统计降级为 `unknown`，再与“如果盲信声明统计会怎样”对比，显式报告误裁剪风险。

因此它回答的是：会裁掉哪些文件、为什么、是否安全、能否回归，以及潜在的全扫描和误裁剪在哪里。

## 3. 快速上手

要求 Python 3.10+，依赖仅为 PyYAML 与 pytest：

```bash
python -m pip install -r requirements.txt
```

运行完整演示（生成 fixture、报告与 SQLite 台账）：

```bash
python -m lakeprune demo
```

默认写入 `runtime/lake/`、`runtime/reports/`、`runtime/lakeprune.db`。也可指定输出目录：

```bash
python -m lakeprune demo --out /tmp/lakeprune-demo
```

运行测试：

```bash
pytest -q
```

启动只读查看器（默认只监听回环地址，端口 0 表示自动选择空闲端口）：

```bash
python -m lakeprune serve --db runtime/lakeprune.db --port 0
```

健康检查：

```bash
curl http://127.0.0.1:<port>/healthz
```

其他常用命令：

```bash
python -m lakeprune init-demo --out /tmp/lake
python -m lakeprune validate-stats --config configs/demo.yaml
python -m lakeprune audit --config configs/demo.yaml --out runtime/reports --fail-on high
python -m lakeprune report --run-id <run-id> --format md --db runtime/lakeprune.db
python -m lakeprune golden --expected tests/golden/expected_report.json --actual runtime/reports/report.json
```

退出码：`0` 表示审计通过或命令成功；`1` 表示配置、输入、查询或运行时错误；`2` 表示存在达到 `--fail-on` 阈值的风险。`demo` 固定返回 `0`，便于生成素材；CI 卡点请使用 `audit --fail-on high`。

## 4. 数据模型

### 4.1 目录约定

湖根目录（`lake_root`）下每张表一个目录，表目录内包含 `_manifest.json` 和数据文件。分区使用 Hive 风格的多级目录：

```text
fixtures/lake/
  readings/
    _manifest.json
    dt=2024-01-01/region=r1/part-0001.csv
    dt=2024-01-01/region=r2/part-0001.csv
  events/
    _manifest.json
    dt=2024-01-02/region=r1/part-0001.csv
  stations/
    _manifest.json
    data.jsonl
```

空值分区使用 `__HIVE_DEFAULT_PARTITION__`。manifest 中的路径一律是相对 POSIX 路径；读取时拒绝空路径、反斜杠、绝对路径、`..` 越界和 symlink 逃逸。

### 4.2 `_manifest.json`

```json
{
  "table": "readings",
  "format": "csv",
  "partition_columns": ["dt", "region"],
  "columns": {"station_id": "string", "value": "float", "humidity": "float", "dt": "date", "region": "string"},
  "partitions": [{"dt": "2024-01-01", "region": "r1"}],
  "files": [
    {
      "path": "dt=2024-01-01/region=r1/part-0001.csv",
      "partition": {"dt": "2024-01-01", "region": "r1"},
      "row_count": 4,
      "size_bytes": 123,
      "sha256": "<64 lowercase hex>",
      "columns": {
        "value": {"type": "float", "min": 1.0, "max": 9.5, "null_count": 0}
      }
    }
  ]
}
```

约定：

- `columns` 是表级 schema，类型必须是 `string`、`int`、`float`、`date`、`datetime`、`bool`；
- 分区列也应在 schema 中声明，且必须在 `partition_columns` 中列出；
- 文件 `partition` 必须与路径中解析出的键值一致，否则记 `TYPE_DRIFT`；
- 数据文件格式只支持 `csv` 与 `jsonl`；CSV 使用首行表头，空字符串按 NULL 处理；JSONL 每行一个对象；
- 文件统计可故意缺失或损坏，用于演示 `MISSING_STATS`、`STATS_MISMATCH`、`TYPE_DRIFT` 等风险；
- manifest 指纹由规范化后的表结构、分区、文件路径、大小、哈希和统计计算，用于报告与 golden 对比。

### 4.3 查询 YAML

```yaml
queries:
  - query_id: q_readings_hot_day
    table: readings
    select: [station_id, value]
    expected_columns: [station_id, value]
    where: "dt = '2024-01-02' AND region = 'r1' AND value > 40.0"
    description: 检测读数高温查询
```

`query_id` 必须唯一；查询按 `query_id` 排序执行，报告与台账排序稳定。

## 5. 谓词支持范围

支持：`= != > >= < <=`、`IN`、`BETWEEN ... AND ...`、`IS NULL`、`IS NOT NULL`、`AND`、`OR`、`NOT` 和括号。支持 `string`、`int`、`float`、`date`、`datetime`、`bool`；日期格式固定为 `YYYY-MM-DD`，日期时间格式固定为 `YYYY-MM-DDTHH:MM:SS`，不使用系统时区。

明确不支持并拒绝：函数调用（如 `lower(status)`）、子查询、`LIKE` / `ILIKE` / 正则、SQL 片段拼接、以 `NULL` 做普通比较、空 `IN` 列表、尾逗号 `IN`、反向 `BETWEEN`、未知字段、类型不兼容的 literal、bool 排序比较。

解析使用自研 lexer + 递归下降 parser，生成显式 AST；不对用户表达式使用 `eval` / `exec`。稳定错误码包括 `E_EMPTY_EXPR`、`E_EMPTY_IN`、`E_BETWEEN_REVERSED`、`E_UNKNOWN_FIELD`、`E_UNSUPPORTED_FUNCTION`、`E_UNSUPPORTED_SYNTAX`、`E_NULL_COMPARISON`、`E_TYPE_MISMATCH` 等。详见 `docs/PREDICATE.md`。

## 6. 风险分类与退出码

| 风险码 | 级别 | 触发条件 |
| --- | --- | --- |
| `MIS_PRUNE_RISK` | high | 若盲信声明统计本会裁掉文件，但统计不可信，实际保留为扫描候选 |
| `FULL_SCAN_RISK` | warn | 谓词无法裁剪任何文件，或分区列缺失导致无法利用分区 |
| `MISSING_STATS` | warn | 文件缺少某列的声明统计 |
| `STATS_MISMATCH` | high | 声明统计与重算结果不一致，或数据完整性校验失败 |
| `TYPE_DRIFT` | high | 分区值或列值违反声明类型；manifest 统计引用未知列 |
| `NULL_SEMANTICS` | warn | 空值谓词依赖不可信或缺失的 `null_count` |
| `BOUNDARY_RISK` | warn | 日期/时间、浮点相等、字符串大小写或补零边界可能歧义 |
| `EMPTY_PARTITION` | warn | 分区存在但没有可扫描行 |

每个风险包含稳定 `risk_id`、风险码、级别、查询 ID、表名、文件路径、证据与描述。风险按级别（high → warn → info）、风险码、查询、表、文件、描述和 ID 稳定排序。风险级别与退出码：

- `--fail-on none`：始终 `0`；
- `--fail-on info`：存在 info/warn/high 风险时 `2`；
- `--fail-on warn`：存在 warn/high 风险时 `2`；
- `--fail-on high`：存在 high 风险时 `2`；
- 命令或输入错误返回 `1`。

规则细节见 `docs/RISKS.md`。

## 7. 报告样例与 golden

每次 `audit` 或 `demo` 会写出：

- `report.json`：规范化、稳定排序、带尾随换行的完整证据报告；
- `report.md`：适合归档的 Markdown 报告；
- `report.html`：自包含 HTML，内联 CSS，无 CDN、无脚本、无外部资源；
- SQLite 台账：`runs`、`queries`、`prune_results`、`risks`、`stats_checks`。

JSON 报告包含 `manifest_fingerprint`、表摘要、每个查询的谓词 AST/渲染文本、文件级分区与统计决定、原因、统计快照、风险列表和统计复核检查。Markdown/HTML 同样给出表清单、查询计划、文件决定、风险和非 OK 统计检查。

`golden` 对 JSON 做深度、稳定比较：

```bash
python -m lakeprune golden \
  --expected tests/golden/expected_report.json \
  --actual runtime/reports/report.json
```

相同 fixture 重复运行应得到字节一致的 `report.json` 与 `report.md`。报告不包含运行 ID 或时间戳；时间信息只写入 SQLite。golden 文件位于 `tests/golden/`。

## 8. 离线与虚构数据声明

本项目运行期不发起外部网络请求，不访问真实数据源，不下载资源；Web 查看器只绑定 `127.0.0.1` 或 `::1`，仅提供只读 GET 页面和 `/healthz`。所有 fixture、查询、配置、站点号、设备号、状态文本和标识均为合成数据，使用 `demo-*`、`example.invalid` 风格或本地回环地址语义；不包含真实密钥、令牌、证书、公司名、真实域名或真实外部目标地址。详细合规边界见 `docs/COMPLIANCE.md`。

## 9. 已知限制与后续方向

- 仅支持 CSV/JSONL 复核统计，不支持 Parquet/ORC/Avro footer 或真实对象存储；
- 统计以文件级 min/max/null_count 为粒度，不做列块、页级或 Bloom filter 下推；
- 谓词语言有意保持小而严格，不支持函数、子查询、LIKE、正则和 SQL 拼接；
- 字符串排序按 Python 字节序语义，未做 locale / collation 建模；大小写边界以 `BOUNDARY_RISK` 提示；
- 浮点比较使用声明值，边界情况以 `BOUNDARY_RISK` 提示，不承诺跨引擎完全一致；
- 台账为单机 SQLite，不包含并发调度、分布式执行、权限或公网部署；
- 后续可增加更多文件格式、列块统计、可配置 collation、独立 HTML CSS 静态资源和更强的差异报告。
