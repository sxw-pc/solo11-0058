# 湖裁验台 Audit Report

- Project slug: `lakeprune`
- Schema version: `1.0`
- Manifest fingerprint: `40b843058f6a0987abc1687e43c8304eca473aac141d3526f0638c94bfff6563`

## Summary

| Metric | Value |
|---|---|
| Tables | 3 |
| Data files | 11 |
| Queries | 7 |
| Candidate files (all queries) | 10 |
| Estimated scan bytes | 2252 |
| High risks | 3 |
| Warnings | 12 |

## Tables

| Table | Format | Manifest | Files | Rows | Bytes | Fingerprint |
|---|---|---|---|---|---|---|
| events | csv | events/_manifest.json | 4 | 7 | 506 | b08d78a98516323080e31d22a757f86d5a1e27db0fde8f7a25965dc555c821b6 |
| readings | csv | readings/_manifest.json | 6 | 15 | 995 | 01b5b4bc1a4df9f34b628a954dac70585d906fc430e5a32e73f335efb464c36c |
| stations | jsonl | stations/_manifest.json | 1 | 4 | 467 | 87e7f696cef0f100bf68c2ea98b3299caa4185ae2bec1a1a2bfad541c632c4be |

## Query plans

### q_events_battery (events)

Find healthy or retrying events with enough battery.

- Predicate: `(status IN ('ok', 'retry') AND battery_pct >= 20)`
- Files: 2 / 4 candidates
- Bytes: 322 / 506
- Prune ratio: 0.363636
- Partitions: {'total': 4, 'hit': 0, 'pruned': 0, 'undetermined': 4}

| File | Partition | Partition decision | Stats decision | Kept | Reasons |
|---|---|---|---|---|---|
| dt=2024-01-01/region=r1/part-0001.csv | {'dt': '2024-01-01', 'region': 'r1'} | undetermined | must_not_match | False | battery_pct >= 20 |
| dt=2024-01-01/region=r2/part-0001.csv | {'dt': '2024-01-01', 'region': 'r2'} | undetermined | unknown | True |  |
| dt=2024-01-02/region=r1/part-0001.csv | {'dt': '2024-01-02', 'region': 'r1'} | undetermined | unknown | True |  |
| dt=2024-01-03/region=r1/part-0001.csv | {'dt': '2024-01-03', 'region': 'r1'} | undetermined | must_not_match | False | file has zero rows |

### q_readings_hot_day (readings)

Find hot readings from the latest two days.

- Predicate: `(dt >= '2024-01-02' AND value > 40.0)`
- Files: 1 / 6 candidates
- Bytes: 192 / 995
- Prune ratio: 0.807035
- Partitions: {'total': 6, 'hit': 0, 'pruned': 2, 'undetermined': 4}

| File | Partition | Partition decision | Stats decision | Kept | Reasons |
|---|---|---|---|---|---|
| dt=2024-01-01/region=r1/part-0001.csv | {'dt': '2024-01-01', 'region': 'r1'} | pruned | skipped_partition | False | dt >= '2024-01-02' |
| dt=2024-01-01/region=r2/part-0001.csv | {'dt': '2024-01-01', 'region': 'r2'} | pruned | skipped_partition | False | dt >= '2024-01-02' |
| dt=2024-01-02/region=r1/part-0001.csv | {'dt': '2024-01-02', 'region': 'r1'} | undetermined | unknown | True |  |
| dt=2024-01-02/region=r2/part-0001.csv | {'dt': '2024-01-02', 'region': 'r2'} | undetermined | must_not_match | False | value > 40.0 |
| dt=2024-01-03/region=__HIVE_DEFAULT_PARTITION__/part-0001.csv | {'dt': '2024-01-03', 'region': None} | undetermined | must_not_match | False | value > 40.0 |
| dt=2024-01-03/region=r1/part-0001.csv | {'dt': '2024-01-03', 'region': 'r1'} | undetermined | must_not_match | False | value > 40.0 |

### q_readings_null_humidity (readings)

Find second-day readings with missing humidity.

- Predicate: `(dt = '2024-01-02' AND humidity IS NULL)`
- Files: 1 / 6 candidates
- Bytes: 140 / 995
- Prune ratio: 0.859296
- Partitions: {'total': 6, 'hit': 0, 'pruned': 4, 'undetermined': 2}

| File | Partition | Partition decision | Stats decision | Kept | Reasons |
|---|---|---|---|---|---|
| dt=2024-01-01/region=r1/part-0001.csv | {'dt': '2024-01-01', 'region': 'r1'} | pruned | skipped_partition | False | dt = '2024-01-02' |
| dt=2024-01-01/region=r2/part-0001.csv | {'dt': '2024-01-01', 'region': 'r2'} | pruned | skipped_partition | False | dt = '2024-01-02' |
| dt=2024-01-02/region=r1/part-0001.csv | {'dt': '2024-01-02', 'region': 'r1'} | undetermined | must_not_match | False | humidity IS NULL |
| dt=2024-01-02/region=r2/part-0001.csv | {'dt': '2024-01-02', 'region': 'r2'} | undetermined | unknown | True |  |
| dt=2024-01-03/region=__HIVE_DEFAULT_PARTITION__/part-0001.csv | {'dt': '2024-01-03', 'region': None} | pruned | skipped_partition | False | dt = '2024-01-02' |
| dt=2024-01-03/region=r1/part-0001.csv | {'dt': '2024-01-03', 'region': 'r1'} | pruned | skipped_partition | False | dt = '2024-01-02' |

### q_readings_upper_region (readings)

Demonstrate case-sensitive partition comparison.

- Predicate: `region = 'R1'`
- Files: 0 / 6 candidates
- Bytes: 0 / 995
- Prune ratio: 1.0
- Partitions: {'total': 6, 'hit': 0, 'pruned': 6, 'undetermined': 0}

| File | Partition | Partition decision | Stats decision | Kept | Reasons |
|---|---|---|---|---|---|
| dt=2024-01-01/region=r1/part-0001.csv | {'dt': '2024-01-01', 'region': 'r1'} | pruned | skipped_partition | False | region = 'R1' |
| dt=2024-01-01/region=r2/part-0001.csv | {'dt': '2024-01-01', 'region': 'r2'} | pruned | skipped_partition | False | region = 'R1' |
| dt=2024-01-02/region=r1/part-0001.csv | {'dt': '2024-01-02', 'region': 'r1'} | pruned | skipped_partition | False | region = 'R1' |
| dt=2024-01-02/region=r2/part-0001.csv | {'dt': '2024-01-02', 'region': 'r2'} | pruned | skipped_partition | False | region = 'R1' |
| dt=2024-01-03/region=__HIVE_DEFAULT_PARTITION__/part-0001.csv | {'dt': '2024-01-03', 'region': None} | pruned | skipped_partition | False | region = 'R1' |
| dt=2024-01-03/region=r1/part-0001.csv | {'dt': '2024-01-03', 'region': 'r1'} | pruned | skipped_partition | False | region = 'R1' |

### q_readings_window (readings)

Inspect a two-day measurement window.

- Predicate: `(dt BETWEEN '2024-01-02' AND '2024-01-03' AND value >= 12.5)`
- Files: 4 / 6 candidates
- Bytes: 664 / 995
- Prune ratio: 0.332663
- Partitions: {'total': 6, 'hit': 0, 'pruned': 2, 'undetermined': 4}

| File | Partition | Partition decision | Stats decision | Kept | Reasons |
|---|---|---|---|---|---|
| dt=2024-01-01/region=r1/part-0001.csv | {'dt': '2024-01-01', 'region': 'r1'} | pruned | skipped_partition | False | dt BETWEEN '2024-01-02' AND '2024-01-03' |
| dt=2024-01-01/region=r2/part-0001.csv | {'dt': '2024-01-01', 'region': 'r2'} | pruned | skipped_partition | False | dt BETWEEN '2024-01-02' AND '2024-01-03' |
| dt=2024-01-02/region=r1/part-0001.csv | {'dt': '2024-01-02', 'region': 'r1'} | undetermined | unknown | True |  |
| dt=2024-01-02/region=r2/part-0001.csv | {'dt': '2024-01-02', 'region': 'r2'} | undetermined | must_match | True |  |
| dt=2024-01-03/region=__HIVE_DEFAULT_PARTITION__/part-0001.csv | {'dt': '2024-01-03', 'region': None} | undetermined | must_match | True |  |
| dt=2024-01-03/region=r1/part-0001.csv | {'dt': '2024-01-03', 'region': 'r1'} | undetermined | must_match | True |  |

### q_stations_active (stations)

List active monitoring stations.

- Predicate: `active = true`
- Files: 1 / 1 candidates
- Bytes: 467 / 467
- Prune ratio: 0.0
- Partitions: {'total': 1, 'hit': 0, 'pruned': 0, 'undetermined': 1}

| File | Partition | Partition decision | Stats decision | Kept | Reasons |
|---|---|---|---|---|---|
| data.jsonl | {} | undetermined | maybe_match | True |  |

### q_stations_notes (stations)

List stations with operational notes.

- Predicate: `notes IS NOT NULL`
- Files: 1 / 1 candidates
- Bytes: 467 / 467
- Prune ratio: 0.0
- Partitions: {'total': 1, 'hit': 0, 'pruned': 0, 'undetermined': 1}

| File | Partition | Partition decision | Stats decision | Kept | Reasons |
|---|---|---|---|---|---|
| data.jsonl | {} | undetermined | unknown | True | notes IS NOT NULL |

## Risks

| Level | Code | Query | Table | File | Risk ID | Description |
|---|---|---|---|---|---|---|
| high | MIS_PRUNE_RISK | q_readings_hot_day | readings | dt=2024-01-02/region=r1/part-0001.csv | R-a98e0eb03f96 | untrusted statistics would have pruned a file that remains a scan candidate |
| high | STATS_MISMATCH | - | readings | dt=2024-01-02/region=r1/part-0001.csv | R-90713c5e3cbd | declared statistics are not reliable for column value |
| high | TYPE_DRIFT | - | events | dt=2024-01-02/region=r1/part-0001.csv | R-52918496786f | actual data violates declared type for column battery_pct |
| warn | BOUNDARY_RISK | q_readings_hot_day | readings | - | R-75b8441790cb | date/time, floating-point or case-sensitive comparison may have boundary semantics |
| warn | BOUNDARY_RISK | q_readings_null_humidity | readings | - | R-b39160162a5c | date/time, floating-point or case-sensitive comparison may have boundary semantics |
| warn | BOUNDARY_RISK | q_readings_upper_region | readings | - | R-c54e28131803 | date/time, floating-point or case-sensitive comparison may have boundary semantics |
| warn | BOUNDARY_RISK | q_readings_window | readings | - | R-48b954a6f18e | date/time, floating-point or case-sensitive comparison may have boundary semantics |
| warn | EMPTY_PARTITION | - | events | dt=2024-01-03/region=r1/part-0001.csv | R-5aaef8d9a780 | partition has no scannable rows: dt=2024-01-03;region=r1 |
| warn | FULL_SCAN_RISK | q_stations_active | stations | - | R-a492b96055ab | predicate cannot prune any file; the entire table will be scanned |
| warn | FULL_SCAN_RISK | q_stations_notes | stations | - | R-273601f3e1f3 | predicate cannot prune any file; the entire table will be scanned |
| warn | MISSING_STATS | - | events | dt=2024-01-01/region=r2/part-0001.csv | R-630342cc7a8a | missing file statistics for column battery_pct |
| warn | MISSING_STATS | - | readings | dt=2024-01-02/region=r2/part-0001.csv | R-4dc9ef981986 | missing file statistics for column humidity |
| warn | MISSING_STATS | - | stations | data.jsonl | R-52b4e1ac38a7 | missing file statistics for column notes |
| warn | NULL_SEMANTICS | q_readings_null_humidity | readings | dt=2024-01-02/region=r2/part-0001.csv | R-a31488d550d6 | NULL predicate cannot be resolved because null_count statistics are missing or untrusted |
| warn | NULL_SEMANTICS | q_stations_notes | stations | data.jsonl | R-4f0e72cbc103 | NULL predicate cannot be resolved because null_count statistics are missing or untrusted |

## Statistics audit

| Metric | Value |
|---|---|
| Files checked | 11 |
| Columns checked | 45 |
| Status counts | {'missing_stats': 3, 'ok': 40, 'stats_mismatch': 1, 'type_drift': 1} |
| Untrusted columns | 5 |

### Non-OK statistics checks

| Table | File | Column | Status | Message |
|---|---|---|---|---|
| events | dt=2024-01-01/region=r2/part-0001.csv | battery_pct | missing_stats | declared file statistics are missing for this column |
| events | dt=2024-01-02/region=r1/part-0001.csv | battery_pct | type_drift | actual values violate declared int type; samples=[{'row': 3, 'value': 'retry'}] |
| readings | dt=2024-01-02/region=r1/part-0001.csv | value | stats_mismatch | max: declared=39.0 actual=42.0 |
| readings | dt=2024-01-02/region=r2/part-0001.csv | humidity | missing_stats | declared file statistics are missing for this column |
| stations | data.jsonl | notes | missing_stats | declared file statistics are missing for this column |

## Evidence notes

All file paths are manifest-relative POSIX paths. Every pruning reason comes from the explicit predicate AST and the manifest fingerprint above. Untrusted statistics are downgraded to `unknown` before file pruning.
