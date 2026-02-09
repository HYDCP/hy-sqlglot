# Doris Transpilation Utility Guide

## Overview

`transpile_to_doris` is an enhanced wrapper around `sqlglot.transpile`, specifically designed for transpiling SQL from other dialects (especially PostgreSQL) to Apache Doris.

### Problems Solved

| Problem                             | Original transpile           | transpile_to_doris           |
| ----------------------------------- | ---------------------------- | ---------------------------- |
| Identifier case sensitivity diff    | ❌ `T.id` and `t.id` mismatch | ✅ Auto-unify to lowercase    |
| CAST without alias loses colname    | ❌ `__cast_0`                 | ✅ Auto-add column alias      |
| UNNEST/EXPLODE syntax difference    | ❌ Doris doesn't support      | ✅ Auto-convert LATERAL VIEW  |
| REGEXP_SPLIT_TO_TABLE not supported | ❌ Doris doesn't support      | ✅ Auto-convert LATERAL VIEW  |
| WITH DATA / WITH NO DATA clause     | ❌ Doris doesn't support      | ✅ Auto-remove clause         |
| LATERAL VIEW column ambiguity       | ❌ Runtime error in Doris     | ✅ Auto-fix with table prefix |
| ASCII function conversion           | ❌ ORD(CONVERT(...)) complex  | ✅ Keep ASCII() simple        |
| Date format incompatibility         | ❌ Java format (yyyyMMdd)     | ✅ Convert to MySQL (%Y%m%d)  |
| E-string quotes lost                | ❌ E'/' becomes /             | ✅ Normalize to '/'           |

---

## Quick Start

### Installation

```bash
pip install sqlglot-26.9.4-py3-none-any.whl
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
    read: str = None,                       # Source dialect: "postgres", "spark", "hive", "mysql", etc.
    write: str = "doris",                   # Target dialect, defaults to doris
    normalize_mode: str = "table_full",     # Identifier normalization mode
    auto_alias_cast: bool = True,           # Auto-add alias for CAST
    explode_to_lateral: bool = True,        # EXPLODE to LATERAL VIEW
    regexp_split_to_lateral: bool = True,   # REGEXP_SPLIT_TO_TABLE to LATERAL VIEW
    preserve_ascii: bool = True,            # Preserve ASCII() function
    convert_date_formats: bool = True,      # Convert Java → MySQL date formats
    normalize_strings: bool = True,         # Normalize E-strings
    pretty: bool = False,                   # Format output
    **opts,                                 # Other Generator options
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
# Output: SELECT id, _explode_tmp.tag FROM t LATERAL VIEW EXPLODE(SPLIT_BY_STRING(tags, ',')) _explode_tmp AS tag
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

### 4. REGEXP_SPLIT_TO_TABLE to LATERAL VIEW

PostgreSQL's `REGEXP_SPLIT_TO_TABLE()` can be used directly in SELECT to split strings into rows. Doris requires LATERAL VIEW with `SPLIT_BY_REGEXP()`.

```python
sql = "SELECT id, REGEXP_SPLIT_TO_TABLE(col, ',') AS val FROM t"
result = pg_to_doris(sql)[0]
# Output: SELECT id, _explode_tmp.val FROM t LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(col, ',')) _explode_tmp AS val
```

**Conversion Rules:**

| PostgreSQL                            | Doris                                                 |
| ------------------------------------- | ----------------------------------------------------- |
| `REGEXP_SPLIT_TO_TABLE(str, pattern)` | `LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(str, pattern))` |

**Nested REGEXP_SPLIT_TO_TABLE:**

The function supports nested calls, converting each layer to a separate LATERAL VIEW:

```python
sql = "SELECT REGEXP_SPLIT_TO_TABLE(REGEXP_SPLIT_TO_TABLE(col, '、'), '，') AS val FROM t"
result = pg_to_doris(sql)[0]
# Output: 
# SELECT _explode_tmp_2.val FROM t 
# LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(col, '、')) _explode_tmp_1 AS _nested_col_0 
# LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(_explode_tmp_1._nested_col_0, '，')) _explode_tmp_2 AS val
```

**Real-world Example:**

```python
sql = """SELECT xxx, xxxx, 
  REGEXP_SPLIT_TO_TABLE(
    REGEXP_SPLIT_TO_TABLE(
      REGEXP_REPLACE(interview, '[0-9]', '', 'g'), 
      '、'
    ), 
    '，'
  ) AS interview 
FROM some_table"""

result = pg_to_doris(sql)[0]
# Output:
# SELECT xxx, xxxx, _explode_tmp_2.interview 
# FROM some_table 
# LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(REGEXP_REPLACE(interview, '[0-9]', '', 'g'), '、')) 
#   _explode_tmp_1 AS _nested_col_0 
# LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(_explode_tmp_1._nested_col_0, '，')) 
#   _explode_tmp_2 AS interview
```

```python
# Disable this feature
result = pg_to_doris(sql, regexp_split_to_lateral=False)[0]
```

### 5. Remove WITH DATA / WITH NO DATA Clause

PostgreSQL's `CREATE TABLE AS SELECT ... WITH DATA` is not supported in Doris. The clause is automatically removed.

```python
sql = "CREATE TABLE t1 AS SELECT * FROM source WITH DATA"
result = pg_to_doris(sql)[0]
# Output: CREATE TABLE t1 AS SELECT * FROM source
```

**Conversion Rules:**

| PostgreSQL                                  | Doris                          |
| ------------------------------------------- | ------------------------------ |
| `CREATE TABLE t AS SELECT ... WITH DATA`    | `CREATE TABLE t AS SELECT ...` |
| `CREATE TABLE t AS SELECT ... WITH NO DATA` | `CREATE TABLE t AS SELECT ...` |

This feature is **always enabled** and cannot be disabled, as the clause causes syntax errors in Doris.

### 6. Automatic Column Ambiguity Fix

After converting `REGEXP_SPLIT_TO_TABLE` or `UNNEST` to `LATERAL VIEW`, column name conflicts may occur in `WHERE` clauses. This feature automatically adds table prefixes to resolve ambiguity.

**Problem Scenario:**

```sql
-- After LATERAL VIEW conversion
SELECT ... FROM cost_features_table
LATERAL VIEW EXPLODE(...) tmp AS interview  ← Generates new column 'interview'
WHERE COALESCE(interview, '') ...  ← Ambiguous! Which 'interview'?
```

**Automatic Fix:**

```python
sql = """
SELECT cust_name, REGEXP_SPLIT_TO_TABLE(interview, ',') AS interview
FROM cost_features_table
WHERE NOT COALESCE(interview, '') LIKE '%:%:%'
"""
result = pg_to_doris(sql)[0]
# Output:
# SELECT cust_name, _explode_tmp.interview
# FROM cost_features_table
# LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(interview, ',')) _explode_tmp AS interview
# WHERE NOT COALESCE(cost_features_table.interview, '') LIKE '%:%:%'
#                    ↑ Automatically prefixed with table name
```

This feature is **always enabled** and cannot be disabled, as it prevents runtime errors in Doris.

### 7. Preserve ASCII Function

PostgreSQL's `ascii()` function is transpiled to a complex `ORD(CONVERT(...))` expression by SQLGlot, but Doris natively supports `ASCII()`, so we preserve it.

```python
sql = "SELECT ascii(name) FROM users"
result = pg_to_doris(sql)[0]
# Output: SELECT ASCII(name) FROM users
# Instead of: SELECT ORD(CONVERT(name USING utf32)) FROM users
```

**Conversion Rules:**

| PostgreSQL   | SQLGlot Default (Doris)         | transpile_to_doris (Doris) |
| ------------ | ------------------------------- | -------------------------- |
| `ascii(col)` | `ORD(CONVERT(col USING utf32))` | `ASCII(col)`               |

```python
# Disable this feature
result = pg_to_doris(sql, preserve_ascii=False)[0]
```

### 8. Convert Date Format Patterns

Doris supports both Java-style (`yyyyMMdd`) and MySQL-style (`%Y%m%d`) date formats, but MySQL-style is more standard. We automatically convert Java format to MySQL format for consistency.

```python
sql = "SELECT str_to_date('20200101', 'yyyyMMdd')"
result = pg_to_doris(sql)[0]
# Output: SELECT STR_TO_DATE('20200101', '%Y%m%d')
```

**Format Mapping:**

| Java Format | MySQL Format | Description     |
| ----------- | ------------ | --------------- |
| `yyyy`      | `%Y`         | 4-digit year    |
| `yy`        | `%y`         | 2-digit year    |
| `MM`        | `%m`         | Month (01-12)   |
| `dd`        | `%d`         | Day (01-31)     |
| `HH`        | `%H`         | Hour (00-23)    |
| `mm`        | `%i`         | Minutes (00-59) |
| `ss`        | `%s`         | Seconds (00-59) |

**Example:**
```python
sql = "SELECT str_to_date('2020-01-01 12:30:45', 'yyyy-MM-dd HH:mm:ss')"
result = pg_to_doris(sql)[0]
# Output: SELECT STR_TO_DATE('2020-01-01 12:30:45', '%Y-%m-%d %H:%i:%s')
```

```python
# Disable this feature
result = pg_to_doris(sql, convert_date_formats=False)[0]
```

### 9. Normalize E-Strings

PostgreSQL's E-strings (`E'...'`) support C-style escape sequences. SQLGlot parses them as `ByteString` nodes which lose quotes during generation. We convert them to proper string literals with correct escaping for Doris.

**Simple String Example:**

```python
sql = "SELECT regexp_replace(col, E'/', ',')"
result = pg_to_doris(sql)[0]
# Output: SELECT REGEXP_REPLACE(col, '/', ',')
# Instead of: SELECT REGEXP_REPLACE(col, /, ',')  ← Missing quotes!
```

**Regular Expression Example:**

```python
# PostgreSQL E-string with regex escape sequences
sql = r"SELECT regexp_replace(text, E'\\s+', ' ')"
result = pg_to_doris(sql)[0]
# Output: SELECT REGEXP_REPLACE(text, '\\s+', ' ')
# Doris will parse '\\s+' as '\s+' for the regex engine
```

**Complex Regex Pattern:**

```python
sql = r"""
SELECT regexp_replace(
    raw_log, 
    E'^\\s*(\\w+)\\s*\\[(\\d{4}-\\d{2}-\\d{2})\\]',
    E'[\\1][\\2]'
)
"""
result = pg_to_doris(sql)[0]
# Output: REGEXP_REPLACE(raw_log, '^\\s*(\\w+)\\s*\\[(\\d{4}-\\d{2}-\\d{2})\\]', '[\\1][\\2]')
# All \s, \w, \d escape sequences are correctly preserved
```

**Conversion Rules:**

| PostgreSQL  | Problem                    | transpile_to_doris   | Doris Interprets |
| ----------- | -------------------------- | -------------------- | ---------------- |
| `E'/'`      | Generates `/` (no quotes)  | Generates `'/'`      | `/`              |
| `E'\\s+'`   | Over-escaped: `'\\\\s+'`   | Generates `'\\s+'`   | `\s+` (regex)    |
| `E'\\w+'`   | Over-escaped: `'\\\\w+'`   | Generates `'\\w+'`   | `\w+` (regex)    |
| `E'\\d{4}'` | Over-escaped: `'\\\\d{4}'` | Generates `'\\d{4}'` | `\d{4}` (regex)  |

**Key Points:**
- PostgreSQL `E'\\s'` (double backslash) represents a single backslash in the string
- Doris `'\\s'` (double backslash in SQL) is parsed as `\s` (single backslash) by Doris
- The regex engine receives `\s` which correctly matches whitespace

```python
# Disable this feature
result = pg_to_doris(sql, normalize_strings=False)[0]
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
  _explode_tmp.tag
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

| Parameter                 | Default        | Description                               |
| ------------------------- | -------------- | ----------------------------------------- |
| `normalize_mode`          | `"table_full"` | Normalize table + alias + refs            |
| `auto_alias_cast`         | `True`         | Auto-add CAST alias                       |
| `explode_to_lateral`      | `True`         | EXPLODE to LATERAL VIEW                   |
| `regexp_split_to_lateral` | `True`         | REGEXP_SPLIT_TO_TABLE to LATERAL VIEW     |
| `preserve_ascii`          | `True`         | Keep ASCII() instead of ORD(CONVERT(...)) |
| `convert_date_formats`    | `True`         | Convert Java format → MySQL format        |
| `normalize_strings`       | `True`         | Normalize E-strings with proper quotes    |
| `remove_with_data`        | Always enabled | Remove WITH DATA clause (cannot disable)  |
| `fix_ambiguity`           | Always enabled | Fix column ambiguity (cannot disable)     |

### Disable Enhanced Features

To fully revert to original transpile behavior:

```python
result = transpile_to_doris(
    sql,
    read="postgres",
    normalize_mode="none",
    auto_alias_cast=False,
    explode_to_lateral=False,
    regexp_split_to_lateral=False,
    preserve_ascii=False,
    convert_date_formats=False,
    normalize_strings=False
)
```

---

## Version Info

- **Package Version**: `sqlglot-26.9.4`
- **Python Requirement**: `>= 3.7`
- **Based on**: SQLGlot v26.9.0
- **Features**: 9 automatic conversions for PostgreSQL to Doris migration
