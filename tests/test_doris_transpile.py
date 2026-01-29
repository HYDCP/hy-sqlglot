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


def run_summary():
    """Output test summary"""

    print("\n\n" + "="*70)
    print(" Test Summary")
    print("="*70)
    print("""
Feature Overview:
  - transpile_to_doris() defaults to TABLE_FULL mode
  - Normalizes: table names, table aliases, table references in columns
  - Does NOT modify: column names, quoted identifiers, column aliases
  - Auto-converts SELECT EXPLODE/UNNEST to LATERAL VIEW syntax

Usage:
  from sqlglot.contrib.doris_transpile import transpile_to_doris
  
  # Default mode (recommended)
  result = transpile_to_doris(sql, read="postgres")
  
  # Specify mode
  result = transpile_to_doris(sql, read="postgres", normalize_mode="table_refs")
  
  # Disable EXPLODE to LATERAL VIEW conversion
  result = transpile_to_doris(sql, read="postgres", explode_to_lateral=False)

Available Modes:
  - none: No normalization (original transpile behavior)
  - table_only: Only normalize table names
  - alias_only: Only normalize table aliases
  - table_and_alias: Normalize table names and aliases
  - table_refs: Normalize table aliases + table references in columns
  - table_full: Normalize table names + aliases + table refs in columns (default)
  - all: Normalize all identifiers (including column names)

EXPLODE/UNNEST Conversion:
  - PostgreSQL: SELECT unnest(arr) AS x FROM t
  - Doris:      SELECT tmp.x FROM t LATERAL VIEW EXPLODE(arr) tmp AS x
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

    # Output summary
    run_summary()
