# Doris Transpilation Utility Guide

## Overview

`transpile_to_doris` is an enhanced wrapper around `sqlglot.transpile`, specifically designed for transpiling SQL from other dialects (especially PostgreSQL) to Apache Doris.

### Problems Solved

| Problem                          | Original transpile           | transpile_to_doris          |
| -------------------------------- | ---------------------------- | --------------------------- |
| Identifier case sensitivity diff | ❌ `T.id` and `t.id` mismatch | ✅ Auto-unify to lowercase   |
| CAST without alias loses colname | ❌ `__cast_0`                 | ✅ Auto-add column alias     |
| UNNEST/EXPLODE syntax difference | ❌ Doris doesn't support      | ✅ Auto-convert LATERAL VIEW |

---

## Quick Start

### Installation

```bash
pip install sqlglot-26.9.1.dev1-py3-none-any.whl
```

### Replace Existing Code

**Before:**
```python
import sqlglot

result = sqlglot.transpile(sql, read="postgres", write="doris")
```

**After:**
```python
from sqlglot.contrib.doris_transpile import transpile_to_doris

result = transpile_to_doris(sql, read="postgres")
```

Or use the shortcut method:
```python
from sqlglot.contrib.doris_transpile import pg_to_doris

result = pg_to_doris(sql)
```

---

## API Reference

### transpile_to_doris

```python
def transpile_to_doris(
    sql: str,
    read: str = None,              # Source dialect: "postgres", "spark", "hive", "mysql", etc.
    write: str = "doris",          # Target dialect, defaults to doris
    normalize_mode: str = "table_full",  # Identifier normalization mode
    auto_alias_cast: bool = True,  # Auto-add alias for CAST
    explode_to_lateral: bool = True,  # EXPLODE to LATERAL VIEW
    pretty: bool = False,          # Format output
    **opts,                        # Other Generator options
) -> List[str]:
```

### Shortcut Methods

```python
from sqlglot.contrib.doris_transpile import pg_to_doris, spark_to_doris, hive_to_doris

# PostgreSQL → Doris
pg_to_doris(sql)

# Spark → Doris
spark_to_doris(sql)

# Hive → Doris
hive_to_doris(sql)
```

---

## Feature Details

### 1. Identifier Case Normalization

PostgreSQL identifiers are case-insensitive, but Doris is case-sensitive.

#### Why Convert to Lowercase?

| Dialect    | Unquoted Identifier Rule  | Example                                |
| ---------- | ------------------------- | -------------------------------------- |
| PostgreSQL | Auto-convert to lowercase | `SELECT * FROM TEST` → accesses `test` |
| Oracle     | Auto-convert to uppercase | `SELECT * FROM test` → accesses `TEST` |
| Doris      | Keep as-is                | `SELECT * FROM TEST` → accesses `TEST` |

**Conversion Logic**: Normalize according to source dialect rules to ensure semantic consistency.

```
PostgreSQL: SELECT * FROM TEST    →  actual table: test
                    ↓ convert
Doris:      SELECT * FROM test    →  accesses: test ✅
```

#### Supported Scenarios

```python
from sqlglot.contrib.doris_transpile import pg_to_doris

# Scenario 1: Table alias case inconsistency
sql = "SELECT T.id, t.name FROM test T WHERE T.age > 10"
result = pg_to_doris(sql)[0]
# Output: SELECT t.id, t.`name` FROM test AS t WHERE t.age > 10

# Scenario 2: Table name without alias
sql = "SELECT T2.id FROM T2 WHERE T2.status = 1"
result = pg_to_doris(sql)[0]
# Output: SELECT t2.id FROM t2 WHERE t2.`status` = 1

# Scenario 3: Subquery alias
sql = "SELECT t2.x FROM (SELECT x FROM y) t2 WHERE T2.z = 1"
result = pg_to_doris(sql)[0]
# Output: SELECT t2.x FROM (SELECT x FROM y) AS t2 WHERE t2.z = 1
```

#### Preserve Original Case

If you need to preserve original case (e.g., table name is uppercase in Doris), use quotes:

```sql
-- Force uppercase in PostgreSQL
SELECT * FROM "TEST"

-- After conversion
SELECT * FROM `TEST`  -- Doris accesses TEST table
```

#### Normalization Modes

| Mode              | Description         | Example Output             |
| ----------------- | ------------------- | -------------------------- |
| `none`            | No normalization    | `T.id, t.name FROM TEST T` |
| `table_only`      | Table name only     | `T.id, t.name FROM test T` |
| `alias_only`      | Alias only          | `T.id, t.name FROM TEST t` |
| `table_and_alias` | Table name + alias  | `T.id, t.name FROM test t` |
| `table_refs`      | Alias + column refs | `t.id, t.name FROM TEST t` |
| `table_full`      | **Recommended**     | `t.id, t.name FROM test t` |
| `all`             | All identifiers     | `t.id, t.name FROM test t` |

```python
# Use different modes
result = pg_to_doris(sql, normalize_mode="table_refs")[0]
```

### 2. CAST Auto-Alias

Prevent column name loss in `CREATE TABLE AS SELECT`.

```python
sql = "CREATE TABLE new_t AS SELECT cast(id as int), name FROM old_t"
result = pg_to_doris(sql)[0]
# Output: CREATE TABLE new_t AS SELECT CAST(id AS INT) AS id, name FROM old_t
```

**Rules:**
- ✅ `CAST(col AS type)` → `CAST(col AS type) AS col`
- ⏭️ `CAST(col AS type) AS x` → unchanged (already has alias)
- ⏭️ `CAST(col + 1 AS type)` → unchanged (not a simple column)

```python
# Disable this feature
result = pg_to_doris(sql, auto_alias_cast=False)[0]
```

### 3. EXPLODE/UNNEST to LATERAL VIEW

PostgreSQL's `unnest()` can be used directly in SELECT, but Doris requires LATERAL VIEW syntax.

```python
sql = "SELECT id, unnest(string_to_array(tags, ',')) AS tag FROM t"
result = pg_to_doris(sql)[0]
# Output: SELECT id, tag FROM t LATERAL VIEW EXPLODE(SPLIT_BY_STRING(tags, ',')) _tmp AS tag
```

**Conversion Rules:**

| PostgreSQL                  | Doris                       |
| --------------------------- | --------------------------- |
| `unnest(arr)`               | `LATERAL VIEW EXPLODE(arr)` |
| `string_to_array(col, sep)` | `SPLIT_BY_STRING(col, sep)` |

```python
# Disable this feature
result = pg_to_doris(sql, explode_to_lateral=False)[0]
```

---

## Usage Examples

### Example 1: Simple Query

```python
from sqlglot.contrib.doris_transpile import pg_to_doris

sql = "SELECT T.id, t.name FROM users T WHERE T.age > 18"
print(pg_to_doris(sql)[0])
# SELECT t.id, t.`name` FROM users AS t WHERE t.age > 18
```

### Example 2: Multi-table JOIN

```python
sql = """
SELECT A.id, B.name 
FROM orders A 
JOIN users B ON a.user_id = B.id 
WHERE A.status = 1
"""
print(pg_to_doris(sql, pretty=True)[0])
```

Output:
```sql
SELECT
  a.id,
  b.`name`
FROM orders AS a
JOIN users AS b
  ON a.user_id = b.id
WHERE
  a.`status` = 1
```

### Example 3: Array Expansion

```python
sql = """
CREATE TEMPORARY TABLE result AS
SELECT id, unnest(string_to_array(tags, ',')) AS tag 
FROM articles
WHERE status = 'published'
"""
print(pg_to_doris(sql, pretty=True)[0])
```

Output:
```sql
CREATE TEMPORARY TABLE result AS
SELECT
  id,
  tag
FROM articles
LATERAL VIEW
EXPLODE(SPLIT_BY_STRING(tags, ',')) _explode_tmp AS tag
WHERE
  `status` = 'published'
```

### Example 4: CREATE TABLE AS SELECT with CAST

```python
sql = """
CREATE TABLE report AS
SELECT 
    cast(id as bigint),
    cast(amount as decimal(10,2)),
    name
FROM raw_data
"""
print(pg_to_doris(sql, pretty=True)[0])
```

Output:
```sql
CREATE TABLE report AS
SELECT
  CAST(id AS BIGINT) AS id,
  CAST(amount AS DECIMAL(10, 2)) AS amount,
  `name`
FROM raw_data
```

### Example 5: Complex Nested Subquery

```python
sql = """
CREATE TEMPORARY TABLE result AS
SELECT t3.x, t3.y
FROM (
    SELECT t2.x, t2.y, row_number() over(partition by t2.cat order by t2.score) as rn
    FROM (
        SELECT x, y, cat, score
        FROM source_table
    ) t2
    WHERE T2.status = 1
) t3
WHERE T3.rn = 1
"""
print(pg_to_doris(sql, pretty=True)[0])
```

Output:
```sql
CREATE TEMPORARY TABLE result AS
SELECT
  t3.x,
  t3.y
FROM (
  SELECT
    t2.x,
    t2.y,
    ROW_NUMBER() OVER (PARTITION BY t2.cat ORDER BY t2.score) AS rn
  FROM (
    SELECT
      x,
      y,
      cat,
      score
    FROM source_table
  ) AS t2
  WHERE
    t2.`status` = 1
) AS t3
WHERE
  t3.rn = 1
```

---

## Migration Guide

### Batch Replacement

If your project extensively uses `sqlglot.transpile`, you can replace it like this:

```python
# At project entry point
from sqlglot.contrib.doris_transpile import transpile_to_doris as transpile

# Existing code needs no modification
result = transpile(sql, read="postgres", write="doris")
```

### Compatibility

`transpile_to_doris` signature is compatible with `sqlglot.transpile`, new parameters have defaults:

| Parameter            | Default        | Description                    |
| -------------------- | -------------- | ------------------------------ |
| `normalize_mode`     | `"table_full"` | Normalize table + alias + refs |
| `auto_alias_cast`    | `True`         | Auto-add CAST alias            |
| `explode_to_lateral` | `True`         | EXPLODE to LATERAL VIEW        |

### Disable Enhanced Features

To fully revert to original transpile behavior:

```python
result = transpile_to_doris(
    sql,
    read="postgres",
    normalize_mode="none",
    auto_alias_cast=False,
    explode_to_lateral=False
)
```

---

## Version Info

- **Package Version**: `sqlglot-26.9.1.dev1`
- **Python Requirement**: `>= 3.7`
- **Based on**: SQLGlot v26.9.0
