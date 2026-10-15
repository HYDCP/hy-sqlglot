from sqlglot import ErrorLevel, ParseError, exp, parse_one
from tests.dialects.test_dialect import Validator


class TestDoris(Validator):
    dialect = "doris"

    def test_doris(self):
        self.validate_all(
            "SELECT TO_DATE('2020-02-02 00:00:00')",
            write={
                "doris": "SELECT TO_DATE('2020-02-02 00:00:00')",
                "oracle": "SELECT CAST('2020-02-02 00:00:00' AS DATE)",
            },
        )
        self.validate_all(
            "SELECT MAX_BY(a, b), MIN_BY(c, d)",
            read={
                "clickhouse": "SELECT argMax(a, b), argMin(c, d)",
            },
        )
        self.validate_all(
            "SELECT ARRAY_SUM(x -> x * x, ARRAY(2, 3))",
            read={
                "clickhouse": "SELECT arraySum(x -> x*x, [2, 3])",
            },
            write={
                "clickhouse": "SELECT arraySum(x -> x * x, [2, 3])",
                "doris": "SELECT ARRAY_SUM(x -> x * x, ARRAY(2, 3))",
            },
        )
        self.validate_all(
            "MONTHS_ADD(d, n)",
            read={
                "oracle": "ADD_MONTHS(d, n)",
            },
            write={
                "doris": "MONTHS_ADD(d, n)",
                "oracle": "ADD_MONTHS(d, n)",
            },
        )
        self.validate_all(
            """SELECT JSON_EXTRACT(CAST('{"key": 1}' AS JSONB), '$.key')""",
            read={
                "postgres": """SELECT '{"key": 1}'::jsonb ->> 'key'""",
            },
            write={
                "doris": """SELECT JSON_EXTRACT(CAST('{"key": 1}' AS JSONB), '$.key')""",
                "postgres": """SELECT JSON_EXTRACT_PATH(CAST('{"key": 1}' AS JSONB), 'key')""",
            },
        )
        self.validate_all(
            "SELECT GROUP_CONCAT('aa', ',')",
            read={
                "doris": "SELECT GROUP_CONCAT('aa', ',')",
                "mysql": "SELECT GROUP_CONCAT('aa' SEPARATOR ',')",
                "postgres": "SELECT STRING_AGG('aa', ',')",
            },
        )
        self.validate_all(
            "SELECT LAG(1, 1, NULL) OVER (ORDER BY 1)",
            read={
                "doris": "SELECT LAG(1, 1, NULL) OVER (ORDER BY 1)",
                "postgres": "SELECT LAG(1) OVER (ORDER BY 1)",
            },
        )
        self.validate_all(
            "SELECT LAG(1, 2, NULL) OVER (ORDER BY 1)",
            read={
                "doris": "SELECT LAG(1, 2, NULL) OVER (ORDER BY 1)",
                "postgres": "SELECT LAG(1, 2) OVER (ORDER BY 1)",
            },
        )
        self.validate_all(
            "SELECT LEAD(1, 1, NULL) OVER (ORDER BY 1)",
            read={
                "doris": "SELECT LEAD(1, 1, NULL) OVER (ORDER BY 1)",
                "postgres": "SELECT LEAD(1) OVER (ORDER BY 1)",
            },
        )
        self.validate_all(
            "SELECT LEAD(1, 2, NULL) OVER (ORDER BY 1)",
            read={
                "doris": "SELECT LEAD(1, 2, NULL) OVER (ORDER BY 1)",
                "postgres": "SELECT LEAD(1, 2) OVER (ORDER BY 1)",
            },
        )

    def test_identity(self):
        self.validate_identity("COALECSE(a, b, c, d)")
        self.validate_identity("SELECT CAST(`a`.`b` AS INT) FROM foo")
        self.validate_identity("SELECT APPROX_COUNT_DISTINCT(a) FROM x")

    def test_full_join(self):
        self.validate_all(
            "SELECT * FROM a FULL JOIN b ON a.id = b.id",
            write={
                "doris": "SELECT * FROM a FULL JOIN b ON a.id = b.id",
            },
        )
        self.validate_all(
            "SELECT * FROM a FULL OUTER JOIN b ON a.id = b.id",
            write={
                "doris": "SELECT * FROM a FULL OUTER JOIN b ON a.id = b.id",
            },
        )

    def test_time(self):
        self.validate_identity("TIMESTAMP('2022-01-01')")

    def test_date_trunc_preserves_datetime_expression_in_both_signatures(self):
        values = (
            "event_time",
            "src.`day`",
            "NOW(6)",
            "NULL",
            "CAST('2026-10-15' AS DATE)",
            "CAST(event_time AS DATETIME) - INTERVAL 1 YEAR",
            "STR_TO_DATE('${bizDate+1}', '%Y%m%d') - INTERVAL 1 YEAR",
        )
        for unit in (
            "second",
            "minute",
            "hour",
            "day",
            "week",
            "month",
            "quarter",
            "year",
        ):
            for value in values:
                expected = parse_one(value, read="doris")
                for arguments in (f"{value}, '{unit}'", f"'{unit}', {value}"):
                    with self.subTest(arguments=arguments):
                        tree = parse_one(f"DATE_TRUNC({arguments})", read="doris")
                        self.assertIsInstance(tree, exp.TimestampTrunc)
                        self.assertEqual(tree.this, expected)
                        self.assertEqual(tree.unit.name, unit.upper())
                        self.assertEqual(
                            parse_one(tree.sql("doris"), read="doris"), tree
                        )
        self.validate_all(
            "DATE_TRUNC(src.event_time, 'DAY')",
            write={
                "doris": "DATE_TRUNC(src.event_time, 'DAY')",
                "postgres": "DATE_TRUNC('DAY', src.event_time)",
            },
        )

    def test_add_partition_preserves_unsupported_command_fallback(self):
        for sql in (
            "ALTER TABLE t ADD PARTITION p1 VALUES LESS THAN ('2026-10-15')",
            "ALTER TABLE t ADD PARTITION IF NOT EXISTS p1 VALUES LESS THAN (MAXVALUE)",
            "ALTER TABLE t ADD PARTITION `set` VALUES IN (1, 2)",
            "ALTER TABLE t ADD PARTITION p1",
            "ALTER TABLE t ADD PARTITION p1 VALUES [(1), (2)), ADD PARTITION p2 VALUES LESS THAN (3)",
            "ALTER TABLE t ADD PARTITION p1 VALUES [(1), (2)), ADD COLUMN id INT",
        ):
            for level in (ErrorLevel.RAISE, ErrorLevel.IMMEDIATE):
                with self.subTest(sql=sql, level=level):
                    tree = parse_one(sql, read="doris", error_level=level)
                    self.assertIsInstance(tree, exp.Command)
                    self.assertEqual(tree.sql("doris"), sql)
        self.validate_identity("ALTER TABLE t ADD COLUMN id INT")

    def test_add_half_open_partition_stays_structured_and_strict(self):
        sql = "ALTER TABLE t ADD PARTITION IF NOT EXISTS `set` VALUES [('2026-10-14'), ('2026-10-15'))"
        tree = self.validate_identity(sql)
        self.assertIsInstance(tree, exp.Alter)
        self.assertIsInstance(tree.args["actions"][0], exp.AddPartition)
        for sql in (
            "ALTER TABLE t ADD PARTITION p1 VALUES [",
            "ALTER TABLE t ADD PARTITION p1 VALUES [(1), (2)]",
            "ALTER TABLE t ADD PARTITION p1 VALUES [(1) (2))",
            "ALTER TABLE t ADD PARTITION p1 VALUES [(1), (2)),",
            "ALTER TABLE t ADD PARTITION p1 VALUES [(1), (2)), ADD",
        ):
            with self.subTest(sql=sql), self.assertRaises(ParseError):
                parse_one(sql, read="doris", error_level=ErrorLevel.IMMEDIATE)

    def test_regex(self):
        self.validate_all(
            "SELECT REGEXP_LIKE(abc, '%foo%')",
            write={
                "doris": "SELECT REGEXP(abc, '%foo%')",
            },
        )

    def test_analyze(self):
        self.validate_identity("ANALYZE TABLE tbl")
        self.validate_identity("ANALYZE DATABASE db")
        self.validate_identity("ANALYZE TABLE TBL(c1, c2)")
