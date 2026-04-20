# DELETE 子句里的子查询：Doris 限制与当前实施方案

> **最终方案（已落地）**：方案 B — 把 DELETE WHERE 里的**标量子查询**
> 自动改写为 `DELETE ... USING (派生表) ... WHERE ...`，要求 Doris ≥ 2.0
> （我们目标是 Doris 3.x）。
>
> 实现位置：`sqlglot/contrib/doris_transpile.py` 中的函数
> `rewrite_delete_scalar_subquery`，默认通过 `transpile_to_doris` 自动启用。

---

## 1. 背景：问题是什么

你贴过的 SQL：

```sql
DELETE FROM cdm.xxx
WHERE BUSI_DATE = (SELECT MAX(data_dt) FROM xx.xx)
  AND TASK_CD = 'xx'
```

**PG 合法**，**Doris 不合法**。Doris 从 1.x 到 3.x 都不支持在 DELETE 的 WHERE
子句里直接写子查询；但 Doris 2.0+ 提供了 `DELETE ... USING ...` 语法可以绕开。

> 三类可能出现的子查询形态（本文档只解决第一类）：
>
> 1. **标量子查询**：`col = (SELECT MAX(x) FROM s)` ← **本文档处理**
> 2. **IN 子查询**：`col IN (SELECT x FROM s)` ← 交由其他 transform / Doris 原生支持
> 3. **EXISTS 子查询**：`EXISTS (SELECT ... WHERE ...)` ← 暂不处理

---

## 2. 当前实施：标量子查询 → USING 派生表

### 2.1 改写规则

任何形如

```sql
DELETE FROM tgt
WHERE ...
  AND tgt_col <cmp> (SELECT <expr> FROM inner WHERE ...)
  ...
```

都会被改写为

```sql
DELETE FROM tgt
USING (SELECT <expr> AS _sq_val_i FROM inner WHERE ...) AS _sq_i
WHERE ...
  AND tgt.tgt_col <cmp> _sq_i._sq_val_i
  ...
```

其中 `<cmp>` 可以是 `=` / `<>` / `>` / `<` / `>=` / `<=`，左右操作数顺序都支持。

### 2.2 用户原用例实际输出

```sql
-- IN
DELETE FROM cdm.xxx
WHERE BUSI_DATE = (SELECT MAX(data_dt) FROM xx.xx)
  AND TASK_CD = 'xx'

-- OUT
DELETE FROM cdm.xxx
USING (SELECT MAX(data_dt) AS _sq_val_0 FROM xx.xx) AS _sq_0
WHERE xxx.BUSI_DATE = _sq_0._sq_val_0
  AND xxx.TASK_CD = 'xx'
```

### 2.3 多个标量子查询

```sql
-- IN
DELETE FROM t
WHERE a = (SELECT MAX(x) FROM s1)
  AND b < (SELECT MIN(y) FROM s2)

-- OUT
DELETE FROM t
USING (SELECT MAX(x) AS _sq_val_0 FROM s1) AS _sq_0,
      (SELECT MIN(y) AS _sq_val_1 FROM s2) AS _sq_1
WHERE t.a = _sq_0._sq_val_0
  AND t.b < _sq_1._sq_val_1
```

每个标量子查询各自成为独立派生表；sqlglot 把它们表示为第一个派生表上的
CROSS-JOIN 链，最终生成 `USING a, b, c` 的语法。

### 2.4 子查询原本就带 alias 的情况

```sql
-- IN
DELETE FROM t WHERE a = (SELECT MAX(x) AS v FROM s)

-- OUT
DELETE FROM t USING (SELECT MAX(x) AS v FROM s) AS _sq_0
WHERE t.a = _sq_0.v
```

用户写的 `AS v` 会被尊重，不会被替换成 `_sq_val_0`。

---

## 3. 列前缀：为什么必须自动补

`DELETE FROM cdm.xxx USING (...)` 之后，派生表也在作用域里。
如果原 WHERE 的 `BUSI_DATE`、`TASK_CD` 不加前缀，在有些边界条件下会撞派生表的列名。
因此 transform 会**自动把 WHERE 里所有无前缀的 Column 加上目标表前缀**
（使用 `target_table.alias_or_name`）。

注意生成前缀是 **table name**，不是 `db.table`：
- `DELETE FROM cdm.xxx` → 前缀用 `xxx`（不是 `cdm.xxx`）
- `DELETE FROM cdm.xxx AS t` → 前缀用 `t`

Doris 在 SQL 解析时会自动把 `xxx.col` 正确解析到 `cdm.xxx`，所以这样写是合法的。

---

## 4. 覆盖范围（"做什么"）

| 场景 | 行为 |
|---|---|
| `col = (标量子查询)` / `col <cmp> (标量子查询)` | ✅ 改写 |
| 一条 DELETE 里多个标量子查询 | ✅ 各自独立派生表 |
| 子查询在比较的左操作数：`(SELECT ...) = col` | ✅ 改写 |
| 比较操作符是 `=` / `<>` / `>` / `<` / `>=` / `<=` | ✅ 改写 |
| 子查询带 `AS alias` | ✅ 保留原 alias |
| 目标表已有 alias | ✅ 识别并复用 |

---

## 5. 跳过范围（"不做什么" + 可能打 warning）

| 场景 | 行为 | 原因 |
|---|---|---|
| DELETE 已经有手写 USING | 保留原样 | 用户可能已自行改写，避免重复合并派生表 |
| DELETE 目标不是单个 Table（多表 / 派生表目标） | 保留原样 | 超出该 transform 范围 |
| 子查询返回 2+ 列（`(a,b) = (SELECT x,y FROM s)`） | 跳过 + `logger.warning` | 不是真正的标量子查询，可能是误写 |
| 子查询是 UNION 等非 SELECT | 跳过 + `logger.warning` | 当前实现不处理 set 运算 |
| `IN (SELECT ...)` 子查询 | **完全不动**（本 transform 不管） | 不同 AST 形状，由其他路径处理 |
| `EXISTS (SELECT ...)` 子查询 | **完全不动** | 暂不处理 |
| 不是 DELETE 的 SELECT / UPDATE | **完全不动** | 本 transform 只针对 DELETE |

---

## 6. 可选项：关闭这个 transform

在调用时传参即可关闭：

```python
from sqlglot.contrib.doris_transpile import transpile_to_doris

# 默认: True，自动改写
out = transpile_to_doris(sql, read="postgres")

# 关闭该改写，保留原 SQL 形态（Doris 会报错，便于定位问题）
out = transpile_to_doris(sql, read="postgres", convert_delete_scalar_subquery=False)
```

命令行交互工具同样有开关（见 `examples/try_doris_sql.py`）：
```bash
python3 examples/try_doris_sql.py --no-tuple-in "SQL"  # 关多列 IN
# 当前没有专门的 --no-delete-scalar 开关，如需可单独加
```

---

## 7. 为什么选方案 B（保留历史决策背景）

早期文档对比了 5 种改写方案：

| 方案 | 结论 |
|---|---|
| A. 拆两条 SQL | 需应用层配合，破坏一进一出的 transpile 契约 |
| **B. DELETE USING 改写** | **✅ 选中**，纯 SQL、Doris 2.0+ 稳定支持 |
| C. CTE 改写 | 并没有解决根本问题（WHERE 里还是子查询） |
| D. 只检测 + 警告 | 最保守，但用户需手工改 |
| E. 常量下推 | sqlglot 是静态工具，做不到 |

线上 Doris **3.x**，方案 B 是最干净的路径，兼容性与语义风险都很低。

---

## 8. 已知限制与将来可能扩展

1. **UPDATE 的 WHERE 标量子查询暂未处理**
   Doris UPDATE 也有类似限制，但行为略不同（看目标表类型）。
   如需要，可按相同模式新增 `rewrite_update_scalar_subquery`。

2. **IN / EXISTS 子查询在 DELETE WHERE 里的处理**
   当前不动。Doris 3.x 对 DELETE + IN 子查询的支持情况需实测。
   多列 IN 已由 `rewrite_tuple_in_subquery` 改为 EXISTS。

3. **子查询是 UNION 等复合查询**
   当前跳过。若真实迁移场景常见，可扩展为在 USING 里直接嵌套 UNION 派生表。

4. **派生表别名冲突的极端情况**
   使用 `_sq_0` / `_sq_val_0` 前缀。如果业务表真的叫 `_sq_0`，会撞名。
   大概率不会发生；必要时可以改为 UUID 后缀。

---

## 9. sqlglot AST 快速一瞥（供后续改动参考）

原 PG DELETE 的 AST 骨架：

```
Delete(
  this=Table(db=cdm, this=xxx),
  where=Where(this=And(
    this=EQ(Column(BUSI_DATE), Subquery(Select(Max(data_dt) FROM xx.xx))),
    expression=EQ(Column(TASK_CD), Literal('xx'))
  ))
)
```

改写后的 AST 骨架：

```
Delete(
  this=Table(db=cdm, this=xxx),
  using=Subquery(
    this=Select(expressions=[Alias(Max(...), '_sq_val_0')] FROM xx.xx),
    alias=TableAlias('_sq_0'),
  ),
  where=Where(this=And(
    this=EQ(Column(BUSI_DATE, table=xxx), Column('_sq_val_0', table='_sq_0')),
    expression=EQ(Column(TASK_CD, table=xxx), Literal('xx'))
  ))
)
```

Delete 节点原生支持 `using` 参数（sqlglot 内置字段），多个 USING 通过挂在第一个
USING 节点的 `joins=[Join, Join, ...]` 上表示。

---

## 10. 我该怎么验证？

1. 用交互工具直接跑：
   ```bash
   python3 examples/try_doris_sql.py "DELETE FROM cdm.xxx WHERE BUSI_DATE = (SELECT MAX(data_dt) FROM xx.xx) AND TASK_CD = 'xx'"
   ```

2. 看完整回归（包含所有其它 Doris 适配修复）：
   ```bash
   python3 examples/test_interval_fix.py
   ```

3. 在 Python 里直接调：
   ```python
   from sqlglot.contrib.doris_transpile import transpile_to_doris
   print(transpile_to_doris(sql, read="postgres")[0])
   ```

4. 把输出喂给 Doris 实际执行看是否 OK（这一步只能你来，我这边跑不到 Doris）。
