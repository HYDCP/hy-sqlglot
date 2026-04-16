"""
Test script for drop_sequence_columns in doris_transpile.

Usage:
    python -m sqlglot.contrib.test_drop_sequence

Or run directly:
    python sqlglot/contrib/test_drop_sequence.py
"""

import sys

from sqlglot.contrib.doris_transpile import pg_to_doris, drop_sequence_columns
from sqlglot import parse_one

PASS = 0
FAIL = 0


def check(name: str, sql: str, expected: str, drop: bool = True):
    global PASS, FAIL
    result = pg_to_doris(sql, drop_sequences=drop)[0]
    ok = result == expected
    status = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{status}] {name}")
    if not ok:
        print(f"         Input:    {sql}")
        print(f"         Expected: {expected}")
        print(f"         Got:      {result}")


def main():
    global PASS, FAIL

    # ── 1. NEXTVAL at the beginning of SELECT list ──
    print("\n1. NEXTVAL 在 SELECT 列表开头")
    check(
        "开头位置 - 基本",
        "INSERT INTO t(id, name) SELECT NEXTVAL('seq'), n FROM src",
        "INSERT INTO t (`name`) SELECT n FROM src",
    )
    check(
        "开头位置 - 带 AS 别名",
        "INSERT INTO t(id, data_dt) SELECT NEXTVAL('seq_d') AS id, data_dt FROM tmp",
        "INSERT INTO t (data_dt) SELECT data_dt FROM tmp",
    )
    check(
        "开头位置 - 三列",
        "INSERT INTO t(id, data_dt, node_no) SELECT NEXTVAL('seq_d') AS id, data_dt, txn_node_no FROM tmp_02",
        "INSERT INTO t (data_dt, node_no) SELECT data_dt, txn_node_no FROM tmp_02",
    )

    # ── 2. NEXTVAL at the end of SELECT list ──
    print("\n2. NEXTVAL 在 SELECT 列表末尾")
    check(
        "末尾位置 - 二列",
        "INSERT INTO t(name, id) SELECT n, NEXTVAL('seq') FROM src",
        "INSERT INTO t (`name`) SELECT n FROM src",
    )
    check(
        "末尾位置 - 三列",
        "INSERT INTO t(data_dt, node_no, id) SELECT data_dt, txn_node_no, NEXTVAL('seq_d') AS id FROM tmp_02",
        "INSERT INTO t (data_dt, node_no) SELECT data_dt, txn_node_no FROM tmp_02",
    )

    # ── 3. NEXTVAL in middle ──
    print("\n3. NEXTVAL 在 SELECT 列表中间")
    check(
        "中间位置",
        "INSERT INTO t(a, id, b) SELECT x, NEXTVAL('seq'), y FROM src",
        "INSERT INTO t (a, b) SELECT x, y FROM src",
    )

    # ── 4. GROUP BY with NEXTVAL ──
    print("\n4. GROUP BY 联动移除")
    check(
        "GROUP BY 联动 - 保留其他分组键",
        "INSERT INTO cdm.a_u_d (id, data_dt, node_no) SELECT NEXTVAL('seq_d') AS id, data_dt, txn_node_no FROM tmp_02 GROUP BY NEXTVAL('seq_d'), data_dt",
        "INSERT INTO cdm.a_u_d (data_dt, node_no) SELECT data_dt, txn_node_no FROM tmp_02 GROUP BY data_dt",
    )
    check(
        "GROUP BY 联动 - GROUP BY 完全消失",
        "INSERT INTO t(id) SELECT NEXTVAL('seq_d') FROM tmp GROUP BY NEXTVAL('seq_d')",
        "INSERT INTO t SELECT FROM tmp",
    )
    check(
        "GROUP BY 联动 - 多个分组键保留",
        "INSERT INTO t(id, a, b) SELECT NEXTVAL('s'), a, b FROM src GROUP BY NEXTVAL('s'), a, b",
        "INSERT INTO t (a, b) SELECT a, b FROM src GROUP BY a, b",
    )

    # ── 5. Multiple NEXTVAL in one statement ──
    print("\n5. 多个 NEXTVAL")
    check(
        "两个 NEXTVAL 都移除",
        "INSERT INTO t(id, seq_no, name) SELECT NEXTVAL('s1'), NEXTVAL('s2'), n FROM src",
        "INSERT INTO t (`name`) SELECT n FROM src",
    )

    # ── 6. Schema-qualified table ──
    print("\n6. 带 schema 的表名")
    check(
        "schema.table 格式",
        "INSERT INTO mydb.mytable(id, col) SELECT NEXTVAL('seq'), val FROM src",
        "INSERT INTO mydb.mytable (col) SELECT val FROM src",
    )

    # ── 7. Non-INSERT statements are NOT affected ──
    print("\n7. 非 INSERT 语句不受影响")
    check(
        "普通 SELECT",
        "SELECT NEXTVAL('s') AS id FROM users",
        "SELECT NEXTVAL('s') AS id FROM users",
    )
    check(
        "UPDATE",
        "UPDATE t SET col = NEXTVAL('s')",
        "UPDATE t SET col = NEXTVAL('s')",
    )
    check(
        "DELETE",
        "DELETE FROM t WHERE id = NEXTVAL('s')",
        "DELETE FROM t WHERE id = NEXTVAL('s')",
    )

    # ── 8. INSERT VALUES is NOT affected ──
    print("\n8. INSERT VALUES 不受影响")
    check(
        "INSERT VALUES 保持原样",
        "INSERT INTO t(id, name) VALUES(NEXTVAL('s'), 'abc')",
        "INSERT INTO t (id, `name`) VALUES (NEXTVAL('s'), 'abc')",
    )

    # ── 9. drop_sequences=False ──
    print("\n9. 关闭 drop_sequences 开关")
    check(
        "drop_sequences=False 保持原样",
        "INSERT INTO t(id, name) SELECT NEXTVAL('s'), n FROM src",
        "INSERT INTO t (id, `name`) SELECT NEXTVAL('s'), n FROM src",
        drop=False,
    )

    # ── 10. No NEXTVAL → no change ──
    print("\n10. 无 NEXTVAL 的 INSERT SELECT 不受影响")
    check(
        "无 NEXTVAL",
        "INSERT INTO t(a, b) SELECT x, y FROM src",
        "INSERT INTO t (a, b) SELECT x, y FROM src",
    )

    # ── Summary ──
    total = PASS + FAIL
    print(f"\n{'=' * 50}")
    print(f"Total: {total}  |  PASS: {PASS}  |  FAIL: {FAIL}")
    print(f"{'=' * 50}")

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
