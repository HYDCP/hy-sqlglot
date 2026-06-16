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

    def test_time(self):
        self.validate_identity("TIMESTAMP('2022-01-01')")

    def test_datetimev2(self):
        self.validate_identity("SELECT CAST(col AS DATETIMEV2) FROM t")
        self.validate_identity("SELECT CAST(col AS DATETIMEV2(3)) FROM t")
        self.validate_identity("CREATE TABLE t (dt DATETIMEV2)")
        self.validate_identity("CREATE TABLE t (dt DATETIMEV2(3))")
        self.validate_identity("SELECT CAST(col AS DATETIME) FROM t")
        self.validate_identity("CREATE TABLE t (dt DATETIME(3))")

    def test_drop_table_force(self):
        self.validate_identity("DROP TABLE IF EXISTS test_table FORCE")
        self.validate_identity("DROP TABLE test_table FORCE")
        self.validate_identity(
            "DROP TABLE IF EXISTS schema.table FORCE",
            "DROP TABLE IF EXISTS `schema`.`table` FORCE",
        )

    def test_partition_by_range_values(self):
        self.validate_identity(
            "CREATE TABLE t (dt DATE) ENGINE=OLAP DUPLICATE KEY (dt) PARTITION BY RANGE (dt) (PARTITION p1 VALUES [('2024-01-01'), ('2024-02-01'))) DISTRIBUTED BY HASH (dt) BUCKETS 1"
        )
        self.validate_identity(
            "CREATE TABLE t (dt DATE) ENGINE=OLAP DUPLICATE KEY (dt) PARTITION BY RANGE (dt) (PARTITION p1 VALUES [('2024-01-01'), ('2024-02-01'))) DISTRIBUTED BY HASH (dt) BUCKETS 1 PROPERTIES (replication_num='1')"
        )
        self.validate_identity(
            "CREATE TABLE t (dt DATE) PARTITION BY RANGE (dt) (PARTITION p1 VALUES [('2024-01-01'), ('2024-02-01')), PARTITION p2 VALUES [('2024-02-01'), ('2024-03-01')))"
        )
        self.validate_identity(
            "CREATE TABLE t (dt DATE, city STRING) PARTITION BY RANGE (dt, city) (PARTITION p1 VALUES [('2024-01-01', 'beijing'), ('2024-02-01', 'shanghai')))"
        )

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
