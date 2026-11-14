# 谓词语言与三值逻辑

## 词法与语法

谓词由自研 `predicate_lexer.py` 切分为标识符、字符串、数字、操作符、逗号、括号和负号，再由 `predicate.py` 的递归下降 parser 解析。优先级从低到高为：

```text
OR
AND
NOT
primary        (parentheses | predicate)
predicate      (column [comparison literal | IN (...) | BETWEEN ... AND ... | IS [NOT] NULL])
```

支持操作符：`= != > >= < <=`。支持 `IN`、`BETWEEN`、`IS NULL`、`IS NOT NULL`、`AND`、`OR`、`NOT` 和括号。字段类型为 `string`、`int`、`float`、`date`、`datetime`、`bool`。日期固定 `YYYY-MM-DD`，日期时间固定 `YYYY-MM-DDTHH:MM:SS`，不依赖系统时区或 locale。

字符串使用单引号，内部单引号用两个单引号转义，例如 `'O''Brien'`。整数可带负号；float 可写小数点；bool 写 `true` / `false`。

## AST

AST 节点定义在 `predicate_ast.py`：

- `Literal(value, type_name)` —— 已按列类型 coercion 的字面量；
- `Column(name)` —— 列引用（parser 通常直接嵌入叶节点）；
- `Comparison(column, op, literal)`；
- `InList(column, values)`；
- `Between(column, low, high)`；
- `IsNull(column, negated)`；
- `And(children)` / `Or(children)` / `Not(child)`。

`render()` 生成稳定、可归档的谓词文本；`ast_to_dict()` 生成稳定 JSON AST，进入报告作为证据。解析后的谓词必须至少引用一个列，否则返回 `E_EMPTY_EXPR`。

## 三值逻辑

每个叶节点求值得到真值集合，可能包含 `True`、`False`、`None`（SQL NULL 语义）。组合规则：

- `AND`：任一侧 `False` 则 `False`；否则任一侧 `None` 则 `None`；仅全部 `True` 才 `True`；
- `OR`：任一侧 `True` 则 `True`；否则任一侧 `None` 则 `None`；仅全部 `False` 才 `False`；
- `NOT`：`True ↔ False`，`None` 保持 `None`。

分区阶段使用精确分区赋值；非分区列叶子返回全集 `{True, False, None}` 并标记 `unknown_used`。决策规则：

- 真值集合不含 `True` → `pruned`；
- 真值集合仅为 `{True}` 且未使用 unknown → `match`；
- 其他情况 → `undetermined`。

文件统计阶段对范围、`IN`、`BETWEEN` 和 NULL 谓词做保守包络：只要存在任何可能真值和可能假值就保留 `maybe_match`；缺少或不可信统计返回 `unknown`；零行文件直接 `must_not_match`。`IS NULL` / `IS NOT NULL` 完全由 `null_count` 与行数决定；全空列的范围比较保留 `unknown` 语义证据。

分区列在文件阶段不使用文件统计，而使用精确分区值，避免“分区列在数据文件中为空”导致误裁。空分区、全空列、字符串边界、日期边界和浮点边界分别由 `EMPTY_PARTITION`、`NULL_SEMANTICS`、`BOUNDARY_RISK` 等风险提示。

## 稳定错误码

| 错误码 | 触发条件 |
| --- | --- |
| `E_EMPTY_EXPR` | 空谓词或没有列引用 |
| `E_EMPTY_IN` | `IN ()` 或尾逗号 |
| `E_BETWEEN_REVERSED` | `BETWEEN` 下界大于上界 |
| `E_UNKNOWN_FIELD` | 字段不在表 schema |
| `E_UNSUPPORTED_FUNCTION` | 函数调用 |
| `E_UNSUPPORTED_SYNTAX` | LIKE/正则/子查询/SQL 片段/`IS true` 等 |
| `E_NULL_COMPARISON` | 使用 `= NULL` 而非 `IS NULL` |
| `E_TYPE_MISMATCH` | literal 与列类型不兼容、bool 排序、非法日期 |
| `E_EXPECTED_COLUMN` | 期望列名但遇到其他 token |
| `E_EXPECTED_OPERATOR` | 缺少比较操作符 |
| `E_EXPECTED_PAREN` / `E_EXPECTED_RPAREN` | 括号缺失 |
| `E_EXPECTED_KEYWORD` | 缺少 `AND`、`NULL` 等关键字 |
| `E_EXPECTED_LITERAL` | 缺少 literal |
| `E_LEX_CHARACTER` / `E_LEX_STRING` | 非法字符或未闭合字符串 |
| `E_UNEXPECTED_TOKEN` | 表达式后存在多余 token |

所有错误携带位置和消息，CLI 映射为退出码 `1`。parser 不调用 `eval` / `exec`，`test_line_limits.py` 会检查这一点。
