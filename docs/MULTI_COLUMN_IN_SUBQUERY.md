# 多列 IN 子查询：语法、语义与 Doris 兼容方案

> 本文不涉及代码实现，纯讲解。读完之后你能：
> 1. 看懂 `(a, b) IN (SELECT ...)` 这段语法到底是什么意思
> 2. 知道它和 `EXISTS` 的等价关系
> 3. 理解 NULL 边界情况下两者的微妙差异
> 4. 决定我们后续要做的转写方案要保留哪些语义

---

## 1. 这是什么语法？

完整名字叫 **row constructor IN subquery**（行构造器 IN 子查询），SQL 标准支持，PG / Oracle / DB2 都实现了。

```sql
-- 单列 IN（你应该熟悉）
WHERE cust_id IN (SELECT cust_id FROM black_list)

-- 多列 IN（标准 SQL）
WHERE (cust_id, risk_type) IN (
    SELECT cust_id, risk_type FROM black_list
)
```

**关键点**：左边的 `(cust_id, risk_type)` 不是函数调用，也不是普通的括号分组，而是一个 **row constructor**——一个有顺序的"行值"，要和右边子查询返回的"行"做整体匹配。

---

## 2. 业务场景

回到你贴的例子：

```sql
UPDATE my_table SET status = 'BLOCKED'
WHERE end_dt = STR_TO_DATE('20260420','%Y%m%d')
  AND (cust_id, risk_type) IN (
      SELECT cust_id, risk_type FROM black_list
  )
```

业务含义：**"客户号 + 风险标签同时命中黑名单的记录才更新"**。

注意"**同时**"这个词。它和下面这个**完全不同**的写法语义不同：

```sql
-- ❌ 这个是错的！
WHERE cust_id IN (SELECT cust_id FROM black_list)
  AND risk_type IN (SELECT risk_type FROM black_list)
```

错在哪？这种写法允许：
- 客户 A 的 risk_type=R1 在黑名单里
- 客户 B 的 risk_type=R2 在黑名单里
- 那么客户 A 的 risk_type=R2 也会被命中（即便 (A, R2) 实际并不在黑名单）

**多列 IN 强制要求"行配对"**，单列拆分会丢配对关系，这就是 Doris 没有原生多列 IN 时不能简单拆开的根本原因。

---

## 3. 标准 SQL 里的精确语义

PG/标准定义如下：

```
(a, b) IN (subquery)  ≡  ∃ row r ∈ subquery: (a = r.col1) AND (b = r.col2)
```

翻译成大白话：**子查询里至少存在一行 r，使得 r 的每一列都等于左边对应的列**。

这正是 `EXISTS` 的语义：

```sql
EXISTS (
    SELECT 1 FROM black_list
    WHERE black_list.cust_id = my_table.cust_id
      AND black_list.risk_type = my_table.risk_type
)
```

所以**核心改写思路**：

```
(a, b) IN (SELECT x, y FROM t [WHERE ...])
        ↓
EXISTS (SELECT 1 FROM t [WHERE ...] AND t.x = a AND t.y = b)
```

注意：
- 子查询的 `SELECT x, y` 列名提取出来作为关联条件
- 原来子查询的 `WHERE` 必须**保留并 AND 在新条件之前**
- `SELECT 1` 是惯例（EXISTS 不在意 SELECT 什么，写 `1` 最便宜）

---

## 4. NULL 边界：IN 与 EXISTS 的微妙差异

这是必须搞清楚的部分。SQL 三值逻辑（TRUE / FALSE / NULL）让 IN 和 EXISTS 在 NULL 上行为不完全一致。

### 4.1 正向 IN（无 NOT）

| 场景 | `(a,b) IN (...)` | 等价 `EXISTS` 改写 | 是否一致 |
|---|---|---|---|
| 子查询返回 (1,2)，外层 (1,2) | TRUE | TRUE | ✅ |
| 子查询返回 (1,2)，外层 (1,3) | FALSE | FALSE | ✅ |
| 外层 a 是 NULL | NULL | NULL（NULL = x 是 NULL） | ✅ |
| 子查询某行某列是 NULL | NULL | NULL | ✅ |

**结论**：正向 IN 改写为 EXISTS **完全等价**，可以放心改。

### 4.2 反向 NOT IN（这才是坑）

```sql
WHERE (cust_id, risk_type) NOT IN (SELECT cust_id, risk_type FROM black_list)
```

PG 的 `NOT IN` 行为：**子查询返回的任何一行只要含 NULL，整个 NOT IN 结果就是 NULL（不命中）**。

为什么？因为 `(a,b) NOT IN (x)` 等价于 `(a,b) <> x`，而 `<>` 对 NULL 返回 NULL，再多个 `AND` 只要有一个 NULL 就把整个表达式拖成 NULL。

而 `NOT EXISTS` 不受 NULL 影响：只看子查询有没有返回行，返回行就是 FALSE，不返回就是 TRUE。

**举个栗子**：
```sql
-- black_list 数据：(NULL, 'R1')
SELECT * FROM users WHERE (cust_id, risk_type) NOT IN (SELECT * FROM black_list)
-- PG 结果：一行都不返回（因为 cust_id <> NULL 是 NULL）

SELECT * FROM users WHERE NOT EXISTS (
    SELECT 1 FROM black_list
    WHERE black_list.cust_id = users.cust_id
      AND black_list.risk_type = users.risk_type
)
-- 改写后结果：所有 users 都返回（因为 NULL = anything 是 NULL，子查询一行都不返回）
```

**两个查询的结果可能完全相反**。

### 4.3 怎么解决 NOT IN 的语义保留

有两种思路：

**思路 A：内层加 IS NOT NULL（严格保留 PG 语义）**

```sql
NOT EXISTS (
    SELECT 1 FROM black_list
    WHERE black_list.cust_id = users.cust_id
      AND black_list.risk_type = users.risk_type
      AND black_list.cust_id IS NOT NULL    -- 新增
      AND black_list.risk_type IS NOT NULL  -- 新增
)
```

这样还不够，因为 PG 的 NOT IN 在子查询有 NULL 时**整体**返回 NULL，导致 outer 行被过滤。要完整等价还要加：

```sql
AND NOT EXISTS (SELECT 1 FROM black_list WHERE cust_id IS NULL OR risk_type IS NULL)
```

太啰嗦了，工程上很少这么做。

**思路 B：直接 NOT EXISTS（宽松，但和 PG 严格语义有差异）**

99% 的业务场景里，子查询不会真出现 NULL（黑名单的关键键一般 NOT NULL），所以两种行为实际等价。但要给业务方明确警告，让他们自己判断是否能接受。

**思路 C：发现 NOT IN 直接拒绝转换，让用户手工处理**

最保守，但 Doris 直接报错时你的 ETL 也会失败，所以这就是个"宁可早死"的策略。

我个人建议在我们工具里：
- 默认采用**思路 B**（直接 NOT EXISTS）+ 一行警告日志
- 提供开关 `strict_not_in_null=False`（默认关）允许用户在确实需要时走思路 A 加内层 IS NOT NULL
- 不引入 NOT IN 拒绝转换的逻辑

---

## 5. 子查询带其他子句怎么办

考察几种常见复杂结构：

### 5.1 子查询带 WHERE
```sql
(a, b) IN (SELECT x, y FROM t WHERE flag = 1)
        ↓
EXISTS (SELECT 1 FROM t WHERE flag = 1 AND t.x = a AND t.y = b)
```
✅ 直接 AND 接上即可。

### 5.2 子查询带 JOIN
```sql
(a, b) IN (SELECT t1.x, t2.y FROM t1 JOIN t2 ON ...)
        ↓
EXISTS (SELECT 1 FROM t1 JOIN t2 ON ... WHERE t1.x = a AND t2.y = b)
```
✅ JOIN 保留，关联条件加在 WHERE。

### 5.3 子查询带 GROUP BY / HAVING
```sql
(a, b) IN (SELECT x, y FROM t GROUP BY x, y HAVING COUNT(*) > 1)
```

❗ **这种不能简单 AND 一个 WHERE 上去**——WHERE 比 GROUP BY 更早执行，会改变分组结果。正确做法是包一层子查询：

```sql
EXISTS (
    SELECT 1 FROM (
        SELECT x, y FROM t GROUP BY x, y HAVING COUNT(*) > 1
    ) sub
    WHERE sub.x = a AND sub.y = b
)
```

工程上：识别出子查询有 `GROUP BY`/`HAVING`/`DISTINCT`/`LIMIT`/`UNION` 等任一影响最终结果集的子句时，必须包一层。

### 5.4 子查询返回的不是普通列
```sql
(a, b) IN (SELECT col1 + 1, UPPER(col2) FROM t)
```
✅ EXISTS 关联条件直接用表达式：
```sql
EXISTS (SELECT 1 FROM t WHERE col1 + 1 = a AND UPPER(col2) = b)
```

### 5.5 左侧 tuple 元素是表达式
```sql
WHERE (cust_id || '_v2', risk_type) IN (SELECT k, v FROM t)
        ↓
EXISTS (SELECT 1 FROM t WHERE t.k = cust_id || '_v2' AND t.v = risk_type)
```
✅ 同样直接放进关联条件。

---

## 6. 列名冲突与作用域

**典型坑**：内外层列同名。

```sql
UPDATE my_table m SET v = 1
WHERE (cust_id, risk_type) IN (SELECT cust_id, risk_type FROM black_list)
```

改写后：
```sql
WHERE EXISTS (SELECT 1 FROM black_list
              WHERE black_list.cust_id = cust_id    -- ❌ 歧义！
                AND black_list.risk_type = risk_type)
```

外层的 `cust_id` 在 EXISTS 子查询作用域里也能解析到 `black_list.cust_id`，导致退化成"某行 = 自己"——恒真。

**保险做法**：
- 外层引用强制加表名/别名前缀：`black_list.cust_id = my_table.cust_id`
- 如果原表没有别名，需要从 UPDATE/SELECT 的 FROM 推断主表名
- 内层引用同样加前缀避免和外层混淆

这是我们做 transform 时必须处理的细节。

---

## 7. 性能视角

| 维度 | `(a,b) IN (subquery)` | `EXISTS (相关子查询)` |
|---|---|---|
| 优化器友好度 | 通常优化为 SEMI JOIN | 通常优化为 SEMI JOIN |
| 是否物化子查询 | 取决于子查询大小 | 同 |
| 索引利用 | 看左右两边列是否有索引 | EXISTS 关联条件能用索引则更高效 |

**Doris 实测**：EXISTS 在 Doris 里会被 plan 成 LEFT SEMI JOIN，性能与 IN 相当。改写后**不会变慢**。

---

## 8. 总结决策清单

我们要在 transpile 工具里做的事，按是否需要你拍板分两类：

### 必做（无需选择）
- ✅ 识别 `In(this=Tuple, query=Subquery)` 形状
- ✅ 正向 IN 直接改写为 EXISTS（语义完全等价）
- ✅ 关联条件用表名/别名前缀避免歧义
- ✅ 子查询带 GROUP BY/HAVING/DISTINCT/LIMIT/UNION 时包一层 `(SELECT ...) sub`

### 需要你拍板的
- ❓ NOT IN 的 NULL 严格语义如何处理（思路 A / B / C）
- ❓ 是否要给一个总开关 `convert_tuple_in_subquery: bool = True` 默认开启
- ❓ 子查询是 VALUES 列表（`(a,b) IN ((1,2),(3,4))`）这种字面值用法是否也转写——通常 Doris 这个能直接跑，不需要转

读完之后告诉我你的选择，我再动手。

---

## 9. 直接相关的 sqlglot AST 一瞥

```
In(
  this=Tuple(expressions=[Column(...), Column(...)]),  ← 左侧 row constructor
  query=Subquery(this=Select(...))                      ← 右侧子查询
)
```

判别条件就两点：
1. `In` 节点
2. `In.this` 是 `Tuple`、`In.query` 是 `Subquery`

我们的 transform 就是 `find_all(exp.In)` + 这两个判断 + 节点替换为 `Exists`。
