"""
Doris Transpilation Test Cases

Test various scenarios for transpiling from PostgreSQL to Doris,
focusing on:
1. Identifier case normalization
2. CAST auto-alias feature
3. EXPLODE/UNNEST to LATERAL VIEW conversion

Run with:
    cd /Users/black/code/sqlglot
    python3 tests/test_doris_transpile.py
"""

import sqlglot
from sqlglot.contrib.doris_transpile import (
    transpile_to_doris,
    pg_to_doris,
    IdentifierNormalizeMode,
    explode_to_lateral_view,
)


def run_test(name: str, sql: str, expected_behavior: str = None):
    """Run a single test and output comparison results"""
    print(f"\n{'='*70}")
    print(f"Test: {name}")
    print(f"{'='*70}")
    print(f"Input SQL (PostgreSQL):")
    print(f"  {sql}")
    print()

    # Original transpile (no normalization)
    original = sqlglot.transpile(sql, read="postgres", write="doris")[0]

    # Using transpile_to_doris (default TABLE_FULL mode)
    normalized = transpile_to_doris(sql, read="postgres")[0]

    print(f"Original transpile (may have case issues):")
    print(f"  {original}")
    print()
    print(f"transpile_to_doris (TABLE_FULL mode):")
    print(f"  {normalized}")

    if expected_behavior:
        print()
        print(f"Expected behavior: {expected_behavior}")

    # Check if there are changes
    if original != normalized:
        print()
        print("✅ Fixed case sensitivity issue")
    else:
        print()
        print("⚪ No change (already correct or no modification needed)")

    return original, normalized


def test_drop_table_if_exists_transform():
    """DROP TABLE gets IF EXISTS without affecting other DROP kinds."""
    assert pg_to_doris("DROP TABLE public.T1")[0] == "DROP TABLE IF EXISTS public.t1"
    assert (
        pg_to_doris("DROP TABLE IF EXISTS public.T1")[0]
        == "DROP TABLE IF EXISTS public.t1"
    )
    assert (
        pg_to_doris("DROP TABLE public.T1 CASCADE")[0]
        == "DROP TABLE IF EXISTS public.t1 CASCADE"
    )
    assert pg_to_doris("DROP VIEW public.V1")[0] == "DROP VIEW public.v1"
    assert (
        transpile_to_doris(
            "DROP TABLE public.T1",
            read="postgres",
            auto_add_drop_table_if_exists=False,
        )[0]
        == "DROP TABLE public.t1"
    )


def test_nextval_rewrite_insert_union_all():
    """NEXTVAL is rewritten in each SELECT arm of INSERT ... UNION ALL."""
    sql = """
    INSERT INTO target (id)
    SELECT NEXTVAL() AS id FROM t WHERE a = 1
    UNION ALL
    SELECT NEXTVAL() AS id FROM u WHERE xx = 1
    """

    assert (
        pg_to_doris(sql)[0]
        == "INSERT INTO target (id) SELECT NULL AS id FROM t WHERE a = 1 "
        "UNION ALL SELECT NULL AS id FROM u WHERE xx = 1"
    )


def test_nextval_rewrite_insert_union_distinct_is_left_unchanged():
    """Plain UNION is not rewritten because distinctness can depend on NEXTVAL."""
    sql = """
    INSERT INTO target (id)
    SELECT NEXTVAL() AS id FROM t
    UNION
    SELECT NEXTVAL() AS id FROM u
    """

    assert (
        pg_to_doris(sql)[0]
        == "INSERT INTO target (id) SELECT NEXTVAL() AS id FROM t "
        "UNION SELECT NEXTVAL() AS id FROM u"
    )


def test_nextval_rewrite_insert_select_with_left_join():
    """JOIN sources do not affect top-level NEXTVAL rewrites."""
    sql = """
    INSERT INTO target (id, name)
    SELECT NEXTVAL() AS id, a.name
    FROM a
    LEFT JOIN b ON a.id = b.a_id
    WHERE b.flag = 1
    """

    assert (
        pg_to_doris(sql)[0]
        == "INSERT INTO target (id, `name`) SELECT NULL AS id, a.`name` "
        "FROM a LEFT JOIN b ON a.id = b.a_id WHERE b.flag = 1"
    )


def test_table_alias_reference_uses_case_insensitive_lookup():
    """Mixed-case references should resolve to the normalized table alias."""
    sql = "SELECT * FROM t1 T1 LEFT JOIN db.tb tt ON T1.Dpst = Tt. Dpst"

    assert (
        pg_to_doris(sql)[0]
        == "SELECT * FROM t1 AS t1 LEFT JOIN db.tb AS tt ON t1.Dpst = tt.Dpst"
    )


def test_quoted_table_alias_keeps_case():
    """Quoted table aliases are explicit and should not be case-normalized."""
    assert (
        pg_to_doris('SELECT "T1".id FROM t "T1"')[0]
        == "SELECT `T1`.id FROM t AS `T1`"
    )


def test_table_alias_normalization_preserves_column_case():
    """Only table qualifiers are normalized; column identifiers keep their case."""
    assert (
        pg_to_doris("SELECT T.ColName FROM MyTable T")[0]
        == "SELECT t.ColName FROM mytable AS t"
    )


def test_table_alias_references_normalize_across_clauses():
    """Mixed-case table qualifiers normalize consistently outside JOIN clauses."""
    sql = "SELECT T.Id FROM Foo T WHERE t.Id > 0 GROUP BY T.Id ORDER BY t.Id"

    assert (
        pg_to_doris(sql)[0]
        == "SELECT t.Id FROM foo AS t WHERE t.Id > 0 GROUP BY t.Id ORDER BY t.Id"
    )


def test_nextval_rewrite_insert_select_from_union_subquery():
    """A UNION inside FROM does not block outer SELECT-list NEXTVAL rewrites."""
    sql = """
    INSERT INTO target (id)
    SELECT NEXTVAL() AS id
    FROM (
        SELECT x FROM t WHERE a = 1
        UNION
        SELECT x FROM u WHERE xx = 1
    ) s
    """

    assert (
        pg_to_doris(sql)[0]
        == "INSERT INTO target (id) SELECT NULL AS id FROM (SELECT x FROM t "
        "WHERE a = 1 UNION SELECT x FROM u WHERE xx = 1) AS s"
    )


def test_nextval_rewrite_insert_union_all_with_left_join():
    """JOIN sources do not affect top-level NEXTVAL rewrites in UNION arms."""
    sql = """
    INSERT INTO target (id, name)
    SELECT NEXTVAL() AS id, a.name
    FROM a
    LEFT JOIN b ON a.id = b.a_id
    WHERE b.flag = 1
    UNION ALL
    SELECT NEXTVAL() AS id, c.name
    FROM c
    LEFT JOIN d ON c.id = d.c_id
    WHERE d.flag = 1
    """

    assert (
        pg_to_doris(sql)[0]
        == "INSERT INTO target (id, `name`) SELECT NULL AS id, a.`name` "
        "FROM a LEFT JOIN b ON a.id = b.a_id WHERE b.flag = 1 UNION ALL "
        "SELECT NULL AS id, c.`name` FROM c LEFT JOIN d ON c.id = d.c_id "
        "WHERE d.flag = 1"
    )


def test_cross_join_lateral_unnest_rewrites_to_lateral_view():
    """Doris accepts LATERAL VIEW, not PostgreSQL CROSS JOIN LATERAL UNNEST."""
    sql = """
    SELECT t.id, x.tag
    FROM t
    CROSS JOIN LATERAL unnest(string_to_array(t.tags, ',')) AS x(tag)
    """

    assert (
        pg_to_doris(sql)[0]
        == "SELECT t.id, x.tag FROM t "
        "LATERAL VIEW EXPLODE(SPLIT_BY_STRING(t.tags, ',')) x AS tag"
    )


def test_cross_join_lateral_unnest_subquery_rewrites_to_lateral_view():
    """A simple LATERAL subquery wrapping UNNEST maps to one Doris LATERAL VIEW."""
    sql = """
    SELECT t.id, x.tag
    FROM t
    CROSS JOIN LATERAL (
        SELECT unnest(string_to_array(t.tags, ',')) AS tag
    ) x
    """

    assert (
        pg_to_doris(sql)[0]
        == "SELECT t.id, x.tag FROM t "
        "LATERAL VIEW EXPLODE(SPLIT_BY_STRING(t.tags, ',')) x AS tag"
    )


def test_cross_join_lateral_regexp_split_rewrites_to_lateral_view():
    """FROM LATERAL REGEXP_SPLIT_TO_TABLE should use Doris LATERAL VIEW."""
    sql = """
    SELECT t.id, x.tag
    FROM t
    CROSS JOIN LATERAL regexp_split_to_table(t.tags, ',') AS x(tag)
    """

    assert (
        pg_to_doris(sql)[0]
        == "SELECT t.id, x.tag FROM t "
        "LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(t.tags, ',')) x AS tag"
    )


def test_cross_join_lateral_uncorrelated_subquery_drops_lateral_keyword():
    """Uncorrelated LATERAL subqueries keep CROSS JOIN semantics without LATERAL."""
    sql = "SELECT t.id, x.v FROM t CROSS JOIN LATERAL (SELECT 1 AS v) x"

    assert (
        pg_to_doris(sql)[0]
        == "SELECT t.id, x.v FROM t CROSS JOIN (SELECT 1 AS v) AS x"
    )


def test_cross_join_lateral_correlated_values_rewrites_to_lateral_view_array():
    """Single-column correlated VALUES can expand through Doris EXPLODE(ARRAY(...))."""
    sql = """
    SELECT t.id, x.v
    FROM t
    CROSS JOIN LATERAL (VALUES (t.id + 1), (t.id + 2)) AS x(v)
    """

    assert (
        pg_to_doris(sql)[0]
        == "SELECT t.id, x.v FROM t "
        "LATERAL VIEW EXPLODE(ARRAY(t.id + 1, t.id + 2)) x AS v"
    )


def test_cross_join_lateral_multicolumn_values_rewrites_to_struct_explode():
    """Multi-column VALUES expand as an array of named structs."""
    sql = """
    SELECT t.id, x.a, x.b
    FROM t
    CROSS JOIN LATERAL (VALUES (1, 'a'), (2, 'b')) AS x(a, b)
    """

    assert (
        pg_to_doris(sql)[0]
        == "SELECT t.id, x.a, x.b FROM t "
        "LATERAL VIEW EXPLODE(ARRAY(NAMED_STRUCT('a', 1, 'b', 'a'), "
        "NAMED_STRUCT('a', 2, 'b', 'b'))) x AS x"
    )


def test_cross_join_lateral_multicolumn_values_expands_star_projection():
    """f.* should become explicit struct field projections after VALUES rewrite."""
    sql = """
    SELECT g.id, g.flag, g.title
    FROM (
        SELECT t.id, f.*
        FROM t
        CROSS JOIN LATERAL (
            VALUES (t.flag1, 'title1'), (t.flag2, 'title2')
        ) AS f(flag, title)
    ) AS g
    WHERE g.flag = '1'
    """

    assert (
        pg_to_doris(sql)[0]
        == "SELECT g.id, g.flag, g.title FROM (SELECT t.id, f.flag AS flag, "
        "f.title AS title FROM t LATERAL VIEW EXPLODE(ARRAY("
        "NAMED_STRUCT('flag', t.flag1, 'title', 'title1'), "
        "NAMED_STRUCT('flag', t.flag2, 'title', 'title2'))) f AS f) AS g "
        "WHERE g.flag = '1'"
    )


def test_pg_to_date_drops_format_argument_for_doris():
    """PostgreSQL TO_DATE(value, format) maps to Doris TO_DATE(value)."""
    assert (
        pg_to_doris("SELECT to_date('20240101', 'yyyymmdd') AS data_dt")[0]
        == "SELECT TO_DATE('20240101') AS data_dt"
    )
    assert (
        pg_to_doris("SELECT TO_DATE(col, 'YYYYMMDD') AS data_dt FROM t")[0]
        == "SELECT TO_DATE(col) AS data_dt FROM t"
    )


def test_str_to_date_keeps_format_argument_for_doris():
    """STR_TO_DATE keeps its format argument and existing format conversion."""
    assert (
        pg_to_doris("SELECT str_to_date('20240101', 'yyyyMMdd') AS data_dt")[0]
        == "SELECT STR_TO_DATE('20240101', '%Y%m%d') AS data_dt"
    )


def test_interval_multiplication_folds_into_interval_amount():
    """Doris accepts interval amount expressions, not external interval multiplication."""
    assert (
        pg_to_doris(
            "SELECT INTERVAL '1 Month' * avg_stability_time::int AS stability FROM t"
        )[0]
        == "SELECT INTERVAL CAST(avg_stability_time AS INT) MONTH AS stability FROM t"
    )
    assert (
        pg_to_doris("SELECT CURRENT_DATE + (a * b * INTERVAL '1 month') FROM t")[0]
        == "SELECT CURRENT_DATE + (INTERVAL a * b MONTH) FROM t"
    )
    assert (
        pg_to_doris("SELECT CURRENT_DATE + (INTERVAL '2 months' * a) FROM t")[0]
        == "SELECT CURRENT_DATE + (INTERVAL 2 * a MONTH) FROM t"
    )


def test_date_part_day_date_diff_rewrites_to_datediff():
    """PG interval-like date subtraction maps to Doris DATEDIFF."""
    assert (
        pg_to_doris(
            "date_part('day', "
            "date_trunc('year', DATE '2024-05-15') + '12 month' "
            "- date_trunc('year', DATE '2023-05-15'))"
        )[0]
        == "DATEDIFF(DATE_TRUNC(CAST('2024-05-15' AS DATE), 'YEAR') "
        "+ INTERVAL 12 MONTH, DATE_TRUNC(CAST('2023-05-15' AS DATE), 'YEAR'))"
    )

    assert (
        pg_to_doris(
            "SELECT date_part('DAY', "
            "date_trunc('year', DATE '2024-05-15') + '12 MONth' "
            "- date_trunc('year', DATE '2023-05-15')) AS days"
        )[0]
        == "SELECT DATEDIFF(DATE_TRUNC(CAST('2024-05-15' AS DATE), 'YEAR') "
        "+ INTERVAL 12 MONTH, DATE_TRUNC(CAST('2023-05-15' AS DATE), 'YEAR')) AS days"
    )

    assert (
        pg_to_doris(
            "SELECT DATE_PART('day', "
            "date_trunc('month', DATE '2024-05-15') "
            "- date_trunc('MONth', DATE '2024-01-15')) AS days"
        )[0]
        == "SELECT DATEDIFF(DATE_TRUNC(CAST('2024-05-15' AS DATE), 'MONTH'), "
        "DATE_TRUNC(CAST('2024-01-15' AS DATE), 'MONTH')) AS days"
    )


def test_numeric_trunc_rewrites_to_truncate_for_doris():
    """Doris exposes numeric truncation as TRUNCATE(...), not TRUNC(...)."""
    assert (
        pg_to_doris(
            "SELECT trunc((coalesce(index_value, 0) - coalesce(base_value, 0)), 1) "
            "AS diff_value FROM t"
        )[0]
        == "SELECT TRUNCATE((COALESCE(index_value, 0) - COALESCE(base_value, 0)), 1) "
        "AS diff_value FROM t"
    )
    assert (
        pg_to_doris("SELECT trunc(amount) AS amount_trunc FROM t")[0]
        == "SELECT TRUNCATE(amount) AS amount_trunc FROM t"
    )
    assert (
        pg_to_doris("SELECT date_trunc('month', dt) AS m FROM t")[0]
        == "SELECT DATE_TRUNC(dt, 'MONTH') AS m FROM t"
    )
    assert (
        pg_to_doris(
            "SELECT trunc(amount, 1) AS amount_trunc FROM t",
            convert_numeric_trunc=False,
        )[0]
        == "SELECT TRUNC(amount, 1) AS amount_trunc FROM t"
    )


def test_delete_trailing_force_is_removed_for_doris():
    """Doris does not support trailing FORCE on DELETE statements."""
    assert (
        pg_to_doris("DELETE FROM xxx.xxx WHERE data_data = 'xxx' FORCE")[0]
        == "DELETE FROM xxx.xxx WHERE data_data = 'xxx'"
    )
    assert (
        pg_to_doris("DELETE FROM xxx.xxx WHERE data_data = 'xxx' FORCE;")[0]
        == "DELETE FROM xxx.xxx WHERE data_data = 'xxx'"
    )
    assert (
        pg_to_doris("DELETE FROM xxx.xxx WHERE data_data = 'FORCE' FORCE")[0]
        == "DELETE FROM xxx.xxx WHERE data_data = 'FORCE'"
    )
    assert (
        pg_to_doris("DELETE FROM xxx.xxx WHERE data_data = 'FORCE'")[0]
        == "DELETE FROM xxx.xxx WHERE data_data = 'FORCE'"
    )
    assert (
        pg_to_doris("SELECT 'FORCE' AS force_value")[0]
        == "SELECT 'FORCE' AS force_value"
    )


def run_drop_table_if_exists_tests():
    """Run DROP TABLE IF EXISTS compatibility checks."""
    print("\n\n" + "="*70)
    print(" DROP TABLE IF EXISTS Tests")
    print("="*70)

    test_drop_table_if_exists_transform()
    print("✅ DROP TABLE adds IF EXISTS, CASCADE is preserved")


def run_all_tests():
    """Run all test cases"""

    print("\n" + "="*70)
    print(" PostgreSQL → Doris Transpilation Tests")
    print(" Testing identifier case normalization")
    print("="*70)

    # ============================================================
    # 1. Basic scenarios: table alias case inconsistency
    # ============================================================

    run_test(
        "1.1 Table alias case inconsistency (T vs t)",
        "SELECT T.id, t.name FROM test t",
        "T.id and t.name should unify to t.id and t.name"
    )

    run_test(
        "1.2 Uppercase table name",
        "SELECT * FROM TEST",
        "TEST should become test"
    )

    run_test(
        "1.3 Both table name and alias uppercase",
        "SELECT T.ID, T.NAME FROM TEST T",
        "All should become lowercase: t.ID, t.NAME FROM test t"
    )

    run_test(
        "1.4 Mixed case alias",
        "SELECT MyTable.col1, mytable.col2 FROM test MyTable",
        "MyTable and mytable should unify"
    )

    # ============================================================
    # 2. Multi-table JOIN scenarios
    # ============================================================

    run_test(
        "2.1 Two-table JOIN - inconsistent aliases",
        "SELECT A.id, a.name, B.value FROM table1 a JOIN table2 B ON A.id = b.id",
        "A/a and B/b should unify respectively"
    )

    run_test(
        "2.2 Three-table JOIN",
        "SELECT T1.a, t1.b, T2.c, t3.d FROM tab1 T1 JOIN tab2 T2 ON t1.id = t2.id JOIN tab3 t3 ON T2.id = T3.id",
        "All table references should unify"
    )

    run_test(
        "2.3 LEFT/RIGHT JOIN",
        "SELECT A.col, B.col FROM table1 A LEFT JOIN table2 B ON a.id = B.id WHERE A.status = 1",
        "A/a and B/b should unify"
    )

    run_test(
        "2.4 Self-join",
        "SELECT T1.name, T2.name FROM employees T1 JOIN employees T2 ON t1.manager_id = T2.id",
        "T1/t1 and T2/t2 should unify respectively"
    )

    # ============================================================
    # 3. Subquery scenarios
    # ============================================================

    run_test(
        "3.1 FROM subquery",
        "SELECT S.id, s.name FROM (SELECT id, name FROM users) S WHERE S.id > 10",
        "S/s should unify"
    )

    run_test(
        "3.2 Nested subquery",
        "SELECT * FROM (SELECT T.id FROM (SELECT id FROM test) T WHERE t.id > 1) AS outer_t",
        "Inner T/t should unify"
    )

    run_test(
        "3.3 WHERE IN subquery",
        "SELECT T.name FROM users T WHERE t.id IN (SELECT user_id FROM orders)",
        "T/t should unify"
    )

    run_test(
        "3.4 EXISTS subquery",
        "SELECT T.* FROM orders T WHERE EXISTS (SELECT 1 FROM users U WHERE u.id = T.user_id)",
        "T/t and U/u should unify"
    )

    # ============================================================
    # 4. WHERE clause scenarios
    # ============================================================

    run_test(
        "4.1 Inconsistent table reference in WHERE",
        "SELECT T.id FROM test t WHERE T.status = 1 AND t.type = 'A'",
        "T/t should unify"
    )

    run_test(
        "4.2 Complex WHERE conditions",
        "SELECT T.* FROM test T WHERE t.a > 1 AND (T.b < 2 OR t.c = 3) AND T.d IN (1,2,3)",
        "All T/t should unify"
    )

    run_test(
        "4.3 BETWEEN and LIKE",
        "SELECT T.name FROM test T WHERE t.age BETWEEN 18 AND 30 AND T.name LIKE '%test%'",
        "T/t should unify"
    )

    # ============================================================
    # 5. GROUP BY / ORDER BY / HAVING
    # ============================================================

    run_test(
        "5.1 Table reference in GROUP BY",
        "SELECT T.category, COUNT(*) FROM products T GROUP BY t.category",
        "T/t should unify"
    )

    run_test(
        "5.2 Table reference in ORDER BY",
        "SELECT T.name, T.age FROM users T ORDER BY t.age DESC, T.name ASC",
        "T/t should unify"
    )

    run_test(
        "5.3 Table reference in HAVING",
        "SELECT T.dept, SUM(T.salary) FROM employees T GROUP BY t.dept HAVING SUM(t.salary) > 10000",
        "T/t should unify"
    )

    run_test(
        "5.4 GROUP BY + ORDER BY + HAVING",
        "SELECT T.category, COUNT(*) as cnt FROM products T GROUP BY t.category HAVING COUNT(*) > 5 ORDER BY T.category",
        "T/t should unify"
    )

    # ============================================================
    # 6. Aggregate functions and expressions
    # ============================================================

    run_test(
        "6.1 Column references in aggregate functions",
        "SELECT SUM(T.amount), AVG(t.price), MAX(T.qty) FROM orders T",
        "T/t should unify"
    )

    run_test(
        "6.2 CASE WHEN expression",
        "SELECT CASE WHEN T.status = 1 THEN t.name ELSE T.alias END FROM users T",
        "T/t should unify"
    )

    run_test(
        "6.3 Arithmetic expressions",
        "SELECT T.price * t.quantity AS total, T.discount / 100 FROM orders T",
        "T/t should unify"
    )

    run_test(
        "6.4 String functions",
        "SELECT CONCAT(T.first_name, ' ', t.last_name), UPPER(T.email) FROM users T",
        "T/t should unify"
    )

    # ============================================================
    # 7. Alias scenarios
    # ============================================================

    run_test(
        "7.1 Column aliases (should not be modified)",
        "SELECT T.id AS ID, t.name AS NAME FROM test T",
        "Column aliases ID, NAME should not be modified"
    )

    run_test(
        "7.2 Expression aliases",
        "SELECT T.a + t.b AS SUM_VALUE FROM test T",
        "T/t unify, SUM_VALUE unchanged"
    )

    # ============================================================
    # 8. UNION / INTERSECT / EXCEPT
    # ============================================================

    run_test(
        "8.1 UNION",
        "SELECT T.id FROM table1 T WHERE t.status = 1 UNION SELECT T.id FROM table2 T WHERE t.status = 2",
        "T/t in both queries should unify respectively"
    )

    run_test(
        "8.2 UNION ALL",
        "SELECT A.name FROM users A WHERE a.age > 18 UNION ALL SELECT B.name FROM admins B WHERE b.active = 1",
        "A/a and B/b should unify respectively"
    )

    # ============================================================
    # 9. CTE (WITH clause)
    # ============================================================

    run_test(
        "9.1 Simple CTE",
        "WITH cte AS (SELECT T.id, t.name FROM test T) SELECT * FROM cte",
        "T/t inside CTE should unify"
    )

    run_test(
        "9.2 Multiple CTEs",
        "WITH cte1 AS (SELECT A.id FROM t1 A WHERE a.x = 1), cte2 AS (SELECT B.id FROM t2 B WHERE b.y = 2) SELECT * FROM cte1 JOIN cte2 ON cte1.id = cte2.id",
        "Aliases inside each CTE should unify"
    )

    # ============================================================
    # 10. Special identifiers
    # ============================================================

    run_test(
        "10.1 Quoted identifiers (should not be modified)",
        'SELECT "T".id, "t".name FROM "Test" AS "T"',
        "Quoted identifiers should not be modified"
    )

    run_test(
        "10.2 Mixed quoted and unquoted",
        'SELECT T.id, "T".name FROM test T',
        "Unquoted T should normalize, quoted unchanged"
    )

    # Note: Reserved words as identifiers need quotes, otherwise parsing fails
    run_test(
        "10.3 Reserved words as identifiers (quoted)",
        'SELECT T."select", t."from", T."where" FROM test T',
        "T/t should unify"
    )

    # ============================================================
    # 11. Complex real-world scenarios
    # ============================================================

    run_test(
        "11.1 E-commerce order query",
        """SELECT 
            O.order_id, o.order_date, 
            U.username, u.email,
            P.product_name, p.price,
            O.quantity * p.price AS total
        FROM orders O
        JOIN users U ON o.user_id = U.id
        JOIN products P ON O.product_id = p.id
        WHERE o.status = 'completed'
        ORDER BY O.order_date DESC""",
        "O/o, U/u, P/p should unify respectively"
    )

    run_test(
        "11.2 Statistical report query",
        """SELECT 
            D.dept_name,
            COUNT(E.id) as emp_count,
            AVG(e.salary) as avg_salary,
            MAX(E.salary) as max_salary
        FROM departments D
        LEFT JOIN employees E ON d.id = e.dept_id
        WHERE D.active = 1
        GROUP BY d.dept_name
        HAVING COUNT(e.id) > 0
        ORDER BY avg_salary DESC""",
        "D/d and E/e should unify respectively"
    )

    # ============================================================
    # 12. Edge cases
    # ============================================================

    run_test(
        "12.1 No table alias",
        "SELECT id, name FROM test WHERE status = 1",
        "No alias, no modification needed"
    )

    run_test(
        "12.2 All lowercase",
        "SELECT t.id, t.name FROM test t WHERE t.status = 1",
        "Already lowercase, no modification needed"
    )

    run_test(
        "12.3 Empty query",
        "SELECT 1",
        "No table reference"
    )

    run_test(
        "12.4 Star query",
        "SELECT T.* FROM test T WHERE t.id > 0",
        "T.* and t.id should unify"
    )


def run_mode_comparison():
    """Compare effects of different normalization modes"""

    print("\n\n" + "="*70)
    print(" Normalization Mode Comparison")
    print("="*70)

    sql = "SELECT T.ID, t.NAME FROM TEST t WHERE T.AGE > 10"

    print(f"\nInput SQL: {sql}\n")
    print(f"{'Mode':<20} {'Result'}")
    print("-"*70)

    modes = [
        ("none", "No normalization"),
        ("table_only", "Table name only"),
        ("alias_only", "Alias only"),
        ("table_and_alias", "Table name + alias"),
        ("table_refs", "Alias + table refs"),
        ("table_full", "Table name + alias + refs"),
        ("all", "All identifiers"),
    ]

    for mode, desc in modes:
        result = transpile_to_doris(
            sql, read="postgres", normalize_mode=mode)[0]
        print(f"{mode:<20} {result}")
        print(f"  └─ {desc}")


def run_subquery_alias_tests():
    """Test subquery alias and table name without alias normalization"""

    print("\n\n" + "="*70)
    print(" Subquery Alias and Table Name Without Alias Tests")
    print("="*70)

    import re

    tests = [
        # (name, SQL, description)
        ("14.1 Table name without alias",
         "SELECT T2.id, T2.name FROM T2 WHERE T2.status = 1",
         "T2 should all become t2"),

        ("14.2 Multiple tables without alias",
         "SELECT T1.id, T2.name FROM T1 JOIN T2 ON T1.id = T2.ref WHERE T2.active = 1",
         "T1 and T2 should all become lowercase"),

        ("14.3 Subquery alias",
         "SELECT t2.x FROM (SELECT x FROM y) t2 WHERE T2.z = 1",
         "Subquery alias T2 should become t2"),

        ("14.4 Nested subquery alias",
         "SELECT * FROM (SELECT t2.x FROM (SELECT x FROM y) t2 WHERE T2.z = 1) t3 WHERE T3.a = 1",
         "t2 and t3 references should all become lowercase"),

        ("14.5 Complex nesting (real scenario)",
         """SELECT t3.x FROM (
                SELECT t2.x, row_number() over(partition by t2.cat order by t2.score) as rn
                FROM (SELECT x, cat, score FROM src) t2
                WHERE T2.status = 1
            ) t3 WHERE T3.rn = 1""",
         "All T2 and T3 should become t2 and t3"),

        ("14.6 Mixed: some with alias, some without",
         "SELECT a.id, T2.name FROM t1 a JOIN T2 ON a.id = T2.ref WHERE T2.status = 1",
         "a stays, T2 becomes t2"),
    ]

    all_passed = True
    for name, sql, desc in tests:
        print(f"\n{'='*70}")
        print(f"Test: {name}")
        print(f"{'='*70}")
        print(f"Description: {desc}")
        print(f"Input: {sql[:70]}..." if len(sql) > 70 else f"Input: {sql}")

        result = pg_to_doris(sql)[0]
        print(f"Output: {result[:70]}..." if len(
            result) > 70 else f"Output: {result}")

        # Check for remaining uppercase table references
        uppercase_refs = re.findall(r'\b[A-Z][A-Za-z0-9]*\.', result)

        if uppercase_refs:
            print(f"❌ Unconverted uppercase refs: {set(uppercase_refs)}")
            all_passed = False
        else:
            print("✅ All converted to lowercase")

    print("\n" + "="*70)
    if all_passed:
        print("✅ All subquery alias tests passed!")
    else:
        print("❌ Some tests failed")

    return all_passed


def run_explode_to_lateral_tests():
    """Test EXPLODE/UNNEST to LATERAL VIEW conversion"""

    print("\n\n" + "="*70)
    print(" EXPLODE/UNNEST to LATERAL VIEW Tests")
    print("="*70)

    tests = [
        # (name, SQL, description)
        ("13.1 Simple UNNEST",
         "SELECT id, unnest(string_to_array(tags, ',')) AS tag FROM t",
         "unnest should become LATERAL VIEW EXPLODE"),

        ("13.2 With WHERE clause",
         "SELECT id, unnest(string_to_array(tags, ',')) AS tag FROM t WHERE status = 1",
         "LATERAL VIEW should preserve WHERE"),

        ("13.3 UNNEST in subquery",
         "SELECT * FROM (SELECT id, unnest(string_to_array(tags, ',')) AS tag FROM t) h WHERE tag <> ''",
         "UNNEST inside subquery should convert"),

        ("13.4 CREATE TABLE AS SELECT",
         "CREATE TEMPORARY TABLE result AS SELECT id, unnest(string_to_array(tags, ',')) AS tag FROM t",
         "UNNEST in DDL should also convert"),

        ("13.5 Multiple UNNESTs",
         "SELECT id, unnest(arr1) AS a, unnest(arr2) AS b FROM t",
         "Should generate multiple LATERAL VIEWs"),

        ("13.6 UNNEST without alias",
         "SELECT id, unnest(arr) FROM t",
         "Should auto-generate default alias _col"),

        ("13.7 Complex nested scenario",
         """CREATE TEMPORARY TABLE xxx AS 
            SELECT * FROM (
                SELECT xx, unnest(string_to_array(yy, ',')) AS zz 
                FROM t WHERE status = 1
            ) h WHERE zz <> ''""",
         "UNNEST in nested subquery should convert correctly"),
    ]

    for name, sql, desc in tests:
        print(f"\n{'='*70}")
        print(f"Test: {name}")
        print(f"{'='*70}")
        print(f"Input SQL:")
        print(f"  {sql[:80]}..." if len(sql) > 80 else f"  {sql}")
        print(f"Description: {desc}")
        print()

        # Disable LATERAL VIEW conversion
        without_lateral = pg_to_doris(sql, explode_to_lateral=False)[0]
        print(f"Without conversion (explode_to_lateral=False):")
        print(f"  {without_lateral[:100]}..." if len(
            without_lateral) > 100 else f"  {without_lateral}")

        # Enable LATERAL VIEW conversion (default)
        with_lateral = pg_to_doris(sql)[0]
        print(f"With conversion (explode_to_lateral=True):")
        print(f"  {with_lateral[:100]}..." if len(
            with_lateral) > 100 else f"  {with_lateral}")

        # Check for LATERAL VIEW
        if "LATERAL VIEW" in with_lateral:
            print("✅ Converted to LATERAL VIEW syntax")
        else:
            print("⚠️ No LATERAL VIEW detected (may not need conversion)")


def run_regexp_split_tests():
    """Test REGEXP_SPLIT_TO_TABLE to LATERAL VIEW conversion"""

    print("\n\n" + "="*70)
    print(" REGEXP_SPLIT_TO_TABLE to LATERAL VIEW Tests")
    print("="*70)

    tests = [
        # (name, SQL, description)
        ("14.1 Simple REGEXP_SPLIT_TO_TABLE",
         "SELECT id, REGEXP_SPLIT_TO_TABLE(col, ',') AS val FROM t",
         "Simple split should become LATERAL VIEW with SPLIT_BY_REGEXP"),

        ("14.2 Nested REGEXP_SPLIT_TO_TABLE",
         "SELECT id, REGEXP_SPLIT_TO_TABLE(REGEXP_SPLIT_TO_TABLE(col, '、'), '，') AS val FROM t",
         "Nested splits should become multiple LATERAL VIEWs"),

        ("14.3 User real-world scenario",
         """SELECT xxx, xxxx, 
            REGEXP_SPLIT_TO_TABLE(REGEXP_SPLIT_TO_TABLE(REGEXP_REPLACE(interview, '[0-9]', '', 'g'), '、'), '，') AS interview 
            FROM some_table""",
         "Complex nested scenario with REGEXP_REPLACE"),

        ("14.4 With WHERE clause",
         "SELECT id, REGEXP_SPLIT_TO_TABLE(tags, ',') AS tag FROM t WHERE status = 1",
         "LATERAL VIEW should preserve WHERE"),

        ("14.5 In subquery",
         "SELECT * FROM (SELECT id, REGEXP_SPLIT_TO_TABLE(tags, ',') AS tag FROM t) h WHERE tag <> ''",
         "REGEXP_SPLIT_TO_TABLE inside subquery should convert"),

        ("14.6 CREATE TABLE AS SELECT",
         "CREATE TEMPORARY TABLE result AS SELECT id, REGEXP_SPLIT_TO_TABLE(tags, ',') AS tag FROM t",
         "REGEXP_SPLIT_TO_TABLE in DDL should also convert"),

        ("14.7 Triple nested",
         "SELECT REGEXP_SPLIT_TO_TABLE(REGEXP_SPLIT_TO_TABLE(REGEXP_SPLIT_TO_TABLE(col, 'a'), 'b'), 'c') AS val FROM t",
         "Triple nesting should create three LATERAL VIEWs"),
    ]

    for name, sql, desc in tests:
        print(f"\n{'='*70}")
        print(f"Test: {name}")
        print(f"{'='*70}")
        print(f"Input SQL:")
        # Format SQL for readability
        sql_display = ' '.join(sql.split())
        print(f"  {sql_display[:80]}..." if len(
            sql_display) > 80 else f"  {sql_display}")
        print(f"Description: {desc}")
        print()

        # Disable LATERAL VIEW conversion
        without_lateral = pg_to_doris(sql, regexp_split_to_lateral=False)[0]
        print(f"Without conversion (regexp_split_to_lateral=False):")
        print(f"  {without_lateral[:100]}..." if len(
            without_lateral) > 100 else f"  {without_lateral}")

        # Enable LATERAL VIEW conversion (default)
        with_lateral = pg_to_doris(sql)[0]
        print(f"With conversion (regexp_split_to_lateral=True):")
        # Format output for readability
        if "LATERAL VIEW" in with_lateral:
            # Pretty print LATERAL VIEWs on separate lines
            parts = with_lateral.split(" LATERAL VIEW ")
            formatted = parts[0]
            for i, part in enumerate(parts[1:], 1):
                formatted += f"\n  LATERAL VIEW {part}"
            print(f"  {formatted}")
        else:
            print(f"  {with_lateral[:100]}..." if len(
                with_lateral) > 100 else f"  {with_lateral}")

        # Check for LATERAL VIEW and count
        if "LATERAL VIEW" in with_lateral:
            lateral_count = with_lateral.count("LATERAL VIEW")
            print(f"✅ Converted to {lateral_count} LATERAL VIEW(s)")
            # Verify SPLIT_BY_REGEXP is used
            if "SPLIT_BY_REGEXP" in with_lateral:
                print("✅ Using SPLIT_BY_REGEXP function")
            else:
                print("⚠️ SPLIT_BY_REGEXP not found")
        else:
            print("⚠️ No LATERAL VIEW detected (conversion may have failed)")


def run_ascii_preserve_tests():
    """Test ASCII function preservation"""

    print("\n\n" + "="*70)
    print(" ASCII Function Preservation Tests")
    print("="*70)

    tests = [
        # (name, SQL, description)
        ("15.1 Simple ASCII function",
         "SELECT ascii(name) FROM users",
         "Should keep ASCII() instead of ORD(CONVERT(...))"),

        ("15.2 ASCII in WHERE clause",
         "SELECT * FROM users WHERE ascii(name) > 65",
         "ASCII in WHERE should also be preserved"),

        ("15.3 ASCII with CAST",
         "SELECT cast(ascii(name) as varchar) FROM users",
         "ASCII with CAST should be preserved"),
    ]

    for name, sql, desc in tests:
        print(f"\n{'='*70}")
        print(f"Test: {name}")
        print(f"{'='*70}")
        print(f"Input SQL: {sql}")
        print(f"Description: {desc}")
        print()

        # With ASCII preservation (default)
        with_ascii = pg_to_doris(sql)[0]
        print(f"Output:")
        print(f"  {with_ascii}")

        if 'ASCII' in with_ascii and 'ORD' not in with_ascii:
            print("✅ ASCII function preserved")
        else:
            print("⚠️ ASCII not preserved")


def run_date_format_tests():
    """Test date format pattern conversion"""

    print("\n\n" + "="*70)
    print(" Date Format Pattern Conversion Tests")
    print("="*70)

    tests = [
        # (name, SQL, java_format, mysql_format, description)
        ("16.1 Simple date format",
         "SELECT str_to_date('20200101', 'yyyyMMdd')",
         "yyyyMMdd",
         "%Y%m%d",
         "Java format should convert to MySQL format"),

        ("16.2 Date with separators",
         "SELECT str_to_date('2020-01-01', 'yyyy-MM-dd')",
         "yyyy-MM-dd",
         "%Y-%m-%d",
         "Date with dashes should convert"),

        ("16.3 Datetime format",
         "SELECT str_to_date('2020-01-01 12:30:45', 'yyyy-MM-dd HH:mm:ss')",
         "yyyy-MM-dd HH:mm:ss",
         "%Y-%m-%d %H:%i:%s",
         "Full datetime should convert correctly"),

        ("16.4 Short year format",
         "SELECT str_to_date('200101', 'yyMMdd')",
         "yyMMdd",
         "%y%m%d",
         "Short year format should convert"),
    ]

    for name, sql, java_fmt, mysql_fmt, desc in tests:
        print(f"\n{'='*70}")
        print(f"Test: {name}")
        print(f"{'='*70}")
        print(f"Input SQL: {sql}")
        print(f"Description: {desc}")
        print()

        result = pg_to_doris(sql)[0]
        print(f"Output: {result}")

        if mysql_fmt in result:
            print(f"✅ Format converted: {java_fmt} → {mysql_fmt}")
        else:
            print(f"⚠️ Format not converted properly")


def run_estring_tests():
    """Test E-string normalization"""

    print("\n\n" + "="*70)
    print(" E-String Normalization Tests")
    print("="*70)

    tests = [
        # (name, SQL, description, expected_pattern)
        ("17.1 E-string with slash",
         "SELECT regexp_replace(col, E'/', ',')",
         "E-string with slash should keep quotes",
         "'/'"),

        ("17.2 E-string with comma",
         "SELECT regexp_replace(col, ',', E',')",
         "E-string in different position",
         "','"),

        ("17.3 Multiple E-strings",
         "SELECT regexp_replace(regexp_replace(col, E'/', ','), E',', ';')",
         "Multiple E-strings should all be normalized",
         "'/'"),

        ("17.4 E-string in WHERE",
         "SELECT * FROM t WHERE col = E'test'",
         "E-string in WHERE clause",
         "'test'"),

        ("17.5 E-string with regex \\s",
         r"SELECT regexp_replace(col, E'\\s+', ' ')",
         "E-string with \\s should output correct escaping",
         "'\\\\s+'"),

        ("17.6 E-string with regex \\w",
         r"SELECT regexp_replace(col, E'\\w+', '_')",
         "E-string with \\w should output correct escaping",
         "'\\\\w+'"),

        ("17.7 Complex regex pattern",
         r"SELECT regexp_replace(raw_log, E'^\\s*(\\w+)\\s*\\[(\\d{4})\\]', E'[\\1][\\2]')",
         "Complex regex with multiple escape sequences",
         "'^\\\\s*(\\\\w+)'"),
    ]

    for name, sql, desc, expected_pattern in tests:
        print(f"\n{'='*70}")
        print(f"Test: {name}")
        print(f"{'='*70}")
        print(f"Input SQL: {sql}")
        print(f"Description: {desc}")
        print()

        result = pg_to_doris(sql)[0]
        print(f"Output: {result}")

        # Check for expected pattern
        if expected_pattern in result:
            print(
                f"✅ E-strings normalized correctly - contains: {expected_pattern}")
        else:
            print(f"⚠️ Expected pattern not found: {expected_pattern}")


def run_summary():
    """Output test summary"""

    print("\n\n" + "="*70)
    print(" Test Summary")
    print("="*70)
    print("""
Feature Overview (10 automatic conversions):
  1. Table/alias case normalization - Unifies T.id and t.id to t.id
  2. CAST auto-alias - Prevents __cast_0 column names
  3. UNNEST → LATERAL VIEW - Converts PostgreSQL UNNEST syntax
  4. REGEXP_SPLIT_TO_TABLE → LATERAL VIEW - Supports nested calls
  5. Remove WITH DATA clause - Strips PostgreSQL-only syntax
  6. Fix LATERAL VIEW ambiguity - Adds table prefixes to resolve conflicts
  7. Preserve ASCII function - Keeps ASCII() instead of ORD(CONVERT(...))
  8. Convert date formats - Java format (yyyyMMdd) → MySQL format (%Y%m%d)
  9. Normalize E-strings - Fixes PostgreSQL E-string quotes
  10. DROP TABLE IF EXISTS - Adds idempotent IF EXISTS guard

Usage:
  from sqlglot.contrib.doris_transpile import transpile_to_doris
  
  # Default mode (all features enabled)
  result = transpile_to_doris(sql, read="postgres")
  
  # Disable specific features
  result = transpile_to_doris(
      sql, 
      read="postgres",
      explode_to_lateral=False,      # Disable EXPLODE conversion
      preserve_ascii=False,           # Disable ASCII preservation
      convert_date_formats=False,     # Disable date format conversion
      normalize_strings=False         # Disable E-string normalization
  )

Available Modes:
  - none: No normalization (original transpile behavior)
  - table_only: Only normalize table names
  - alias_only: Only normalize table aliases
  - table_and_alias: Normalize table names and aliases
  - table_refs: Normalize table aliases + table references in columns
  - table_full: Normalize table names + aliases + table refs in columns (default)
  - all: Normalize all identifiers (including column names)

Key Conversions:
  - UNNEST:              SELECT unnest(arr) AS x FROM t
                      → SELECT tmp.x FROM t LATERAL VIEW EXPLODE(arr) tmp AS x
  
  - REGEXP_SPLIT_TO_TABLE: SELECT REGEXP_SPLIT_TO_TABLE(col, ',') AS x FROM t
                        → SELECT tmp.x FROM t LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(col, ',')) tmp AS x
  
  - ASCII:               SELECT ascii(name) FROM t
                      → SELECT ASCII(name) FROM t  (not ORD(CONVERT(...)))
  
  - Date Format:         STR_TO_DATE('20200101', 'yyyyMMdd')
                      → STR_TO_DATE('20200101', '%Y%m%d')
  
  - E-String:            regexp_replace(col, E'/', ',')
                      → REGEXP_REPLACE(col, '/', ',')

  - DROP TABLE:          DROP TABLE t
                      → DROP TABLE IF EXISTS t
""")


if __name__ == "__main__":
    # Run all tests
    run_all_tests()

    # Compare different modes
    run_mode_comparison()

    # Subquery alias and table name without alias tests
    run_subquery_alias_tests()

    # EXPLODE to LATERAL VIEW tests
    run_explode_to_lateral_tests()

    # REGEXP_SPLIT_TO_TABLE to LATERAL VIEW tests
    run_regexp_split_tests()

    # New features tests
    run_ascii_preserve_tests()
    run_date_format_tests()
    run_estring_tests()
    run_drop_table_if_exists_tests()

    # Output summary
    run_summary()
