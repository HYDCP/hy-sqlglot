from sqlglot import ErrorLevel, ParseError, UnsupportedError, exp, parse_one
from tests.dialects.test_dialect import Validator


class TestADB(Validator):
    dialect = "adb"

    def test_truncate_partition_structure_and_roundtrip(self):
        for source, target in (
            (
                "ALTER TABLE raw.t TRUNCATE PARTITION p1",
                "TRUNCATE TABLE raw.t PARTITION(p1)",
            ),
            (
                'ALTER TABLE "raw"."T" TRUNCATE PARTITION "set"',
                "TRUNCATE TABLE `raw`.`T` PARTITION(`set`)",
            ),
        ):
            tree = self.validate_identity(source)
            self.assertIsInstance(tree, exp.TruncateTable)
            self.assertIsInstance(tree.args["partition"], exp.Partition)
            original = tree.copy()
            self.assertEqual(
                tree.sql("doris", unsupported_level=ErrorLevel.RAISE), target
            )
            self.assertEqual(tree, original)
            target_tree = parse_one(
                target, read="doris", error_level=ErrorLevel.IMMEDIATE
            )
            self.assertIsInstance(
                target_tree.args["partition"].expressions[0], exp.Identifier
            )
            self.assertEqual(target_tree.sql("adb"), source)
        self.validate_identity("TRUNCATE TABLE raw.t")

    def test_truncate_for_preserves_selector_without_guessing_name(self):
        for value in ("DATE '20261014'", "'20261014'", "1, 2", "DATE '${day + 1}'"):
            source = f"ALTER TABLE raw.t TRUNCATE PARTITION FOR ({value})"
            tree = self.parse_one(source)
            self.assertIsInstance(tree, exp.TruncateTable)
            self.assertIsInstance(tree.args["partition"], exp.Tuple)
            regenerated = tree.sql("adb")
            self.assertEqual(parse_one(regenerated, read="adb"), tree)
            with self.assertRaises(UnsupportedError):
                tree.sql("doris", unsupported_level=ErrorLevel.RAISE)

    def test_truncate_partition_options_cannot_become_whole_table(self):
        for source in (
            "ALTER TABLE ONLY raw.t TRUNCATE PARTITION p1",
            "ALTER TABLE IF EXISTS raw.t TRUNCATE PARTITION p1",
            "ALTER VIEW raw.t TRUNCATE PARTITION p1",
            "ALTER TABLE raw.t TRUNCATE PARTITION p1 CASCADE",
            "ALTER TABLE raw.t TRUNCATE PARTITION p1 RESTART IDENTITY",
            "ALTER TABLE raw.t TRUNCATE PARTITION p1, TRUNCATE PARTITION p2",
            "ALTER TABLE raw.t TRUNCATE PARTITION p1, ADD COLUMN x INT",
            "ALTER TABLE raw.t TRUNCATE PARTITION FOR ()",
            "ALTER TABLE raw.t TRUNCATE PARTITION",
        ):
            with self.subTest(source=source):
                try:
                    tree = parse_one(
                        source, read="adb", error_level=ErrorLevel.IMMEDIATE
                    )
                except ParseError:
                    continue
                self.assertIsInstance(tree, exp.Command)

    def test_doris_truncate_generator_rejects_source_options_and_complex_selectors(
        self,
    ):
        for source in (
            "TRUNCATE TABLE t RESTART IDENTITY",
            "TRUNCATE TABLE t CASCADE",
            "TRUNCATE TABLE t, u",
            "TRUNCATE TABLE t PARTITION(p1, p2 + 1)",
            "TRUNCATE TABLE t PARTITION(p1 + 1)",
            "TRUNCATE TABLE t SUBPARTITION(p1)",
        ):
            with self.subTest(source=source):
                tree = parse_one(source, read="doris", error_level=ErrorLevel.IMMEDIATE)
                with self.assertRaises(UnsupportedError):
                    tree.sql("doris", unsupported_level=ErrorLevel.RAISE)

    def test_create_distribution(self):
        for source, target in (
            ("CREATE TABLE t (id INT, payload VARCHAR(8)) DISTRIBUTED BY (id, payload)",
             "CREATE TABLE t (id INT, payload VARCHAR(8)) DISTRIBUTED BY HASH (id, payload)"),
            ("CREATE TABLE t AS SELECT 1 AS id DISTRIBUTED BY (id)",
             "CREATE TABLE t DISTRIBUTED BY HASH (id) AS SELECT 1 AS id"),
            ("CREATE TABLE t AS SELECT id FROM source DISTRIBUTED BY (id)",
             "CREATE TABLE t DISTRIBUTED BY HASH (id) AS SELECT id FROM source"),
            ("CREATE TABLE t AS SELECT id DISTRIBUTED BY (id)",
             "CREATE TABLE t DISTRIBUTED BY HASH (id) AS SELECT id"),
            ("CREATE TABLE t AS SELECT 1 AS id DISTRIBUTED RANDOMLY",
             "CREATE TABLE t DISTRIBUTED BY RANDOM AS SELECT 1 AS id"),
            ('CREATE TABLE "T" AS SELECT 1 AS "set" DISTRIBUTED BY ("set")',
             'CREATE TABLE `T` DISTRIBUTED BY HASH (`set`) AS SELECT 1 AS `set`'),
        ):
            with self.subTest(source=source):
                tree = self.validate_identity(source)
                distribution = tree.args["properties"].expressions[-1]
                self.assertIsInstance(distribution, exp.DistributedByProperty)
                self.assertEqual(tree.sql("doris", unsupported_level=ErrorLevel.RAISE), target)
                self.assertEqual(parse_one(target, read="doris").sql("adb"), source)

    def test_create_storage_properties_retained(self):
        source = "CREATE TABLE t WITH (appendonly=TRUE, custom='literal DISTRIBUTED BY (x)', orientation=column) AS SELECT 1 AS id DISTRIBUTED BY (id)"
        tree = self.validate_identity(source)
        properties = tree.args["properties"].expressions
        self.assertEqual(len(properties), 4)
        self.assertEqual(properties[1].args["value"].this, "literal DISTRIBUTED BY (x)")
        self.assertEqual(tree.sql("adb"), source)
        self.validate_identity('SELECT id AS "DISTRIBUTED" FROM source AS "DISTRIBUTED"')

    def test_create_explicit_population_clause_retained(self):
        for population in ("WITH DATA", "WITH NO DATA"):
            source = f"CREATE TEMPORARY TABLE t AS SELECT 1 AS id {population} DISTRIBUTED BY (id)"
            tree = self.parse_one(source)
            self.assertIsInstance(tree, exp.Create)
            self.assertIn(population, tree.sql("adb"))
            self.assertEqual(tree.args["properties"].expressions[-1].expressions[0].name, "id")

    def test_invalid_create_distribution_is_not_partial_ast(self):
        for source in ("CREATE TABLE t (id INT) DISTRIBUTED BY ()",
                       "CREATE TABLE t (id INT) DISTRIBUTED BY (id+1)",
                       "CREATE TABLE t (id INT) DISTRIBUTED BY (id) trailing"):
            with self.subTest(source=source):
                try:
                    tree = parse_one(source, read="adb", error_level=ErrorLevel.IMMEDIATE)
                except ParseError:
                    continue
                self.assertIsInstance(tree, exp.Command)

    def test_doris_table_properties_round_trip(self):
        source = "CREATE TABLE t (payload VARCHAR(32) NULL, id INT NOT NULL) DISTRIBUTED BY HASH (id) PROPERTIES ('enable_duplicate_without_keys_by_default'='true')"
        tree = parse_one(source, read="doris", error_level=ErrorLevel.IMMEDIATE)
        self.assertIsInstance(tree, exp.Create)
        self.assertEqual(tree.sql("doris"), source)
        self.assertEqual(tree.this.expressions[0].name, "payload")

    def test_range_partition(self):
        source = "ALTER TABLE raw.fact ADD PARTITION d20261014 START ('2026-10-14') INCLUSIVE END ('2026-10-15') EXCLUSIVE"
        target = "ALTER TABLE raw.fact ADD PARTITION d20261014 VALUES [('2026-10-14'), ('2026-10-15'))"
        tree = self.validate_identity(source)
        self.assertIsInstance(tree, exp.Alter)
        self.assertIsInstance(tree.args["actions"][0], exp.AddPartition)
        self.assertEqual(tree.sql("doris", unsupported_level=ErrorLevel.RAISE), target)
        self.assertEqual(parse_one(target, read="doris").sql("adb"), source)

    def test_default_bounds_and_composite(self):
        source = (
            "ALTER TABLE fact ADD PARTITION IF NOT EXISTS p1 START (-1, 2) END (3, 4)"
        )
        target = (
            "ALTER TABLE fact ADD PARTITION IF NOT EXISTS p1 VALUES [('-1', '2'), ('3', '4'))"
        )
        tree = self.parse_one(source)
        bound = tree.args["actions"][0].expression
        self.assertTrue(bound.args["from_inclusive"])
        self.assertFalse(bound.args["to_inclusive"])
        self.assertEqual(tree.sql("doris", unsupported_level=ErrorLevel.RAISE), target)
        self.assertEqual(parse_one(target, read="doris").sql("doris"), target)

    def test_non_half_open_not_silently_changed(self):
        for inclusion in (
            ("EXCLUSIVE", "EXCLUSIVE"),
            ("INCLUSIVE", "INCLUSIVE"),
            ("EXCLUSIVE", "INCLUSIVE"),
        ):
            source = "ALTER TABLE fact ADD PARTITION p1 START (1) {} END (2) {}".format(
                *inclusion
            )
            self.validate_identity(source)
            tree = self.parse_one(source)
            for dialect in ("doris", "postgres"):
                with self.assertRaises(UnsupportedError):
                    tree.sql(dialect, unsupported_level=ErrorLevel.RAISE)

    def test_quoted_partition_names(self):
        source = 'ALTER TABLE "raw"."Fact" ADD PARTITION "set" START (1) END (2)'
        target = "ALTER TABLE `raw`.`Fact` ADD PARTITION `set` VALUES [('1'), ('2'))"
        self.assertEqual(self.parse_one(source).sql("doris"), target)

    def test_drop_named_and_selector(self):
        self.validate_identity("ALTER TABLE raw.fact DROP PARTITION IF EXISTS p1")
        self.validate_identity('ALTER TABLE "raw"."Fact" DROP PARTITION "set"')
        self.validate_identity("ALTER TABLE raw.fact DROP PARTITION FOR (1, 2)")
        selector = self.parse_one("ALTER TABLE raw.fact DROP PARTITION FOR (1, 2)")
        self.assertIsInstance(selector.args["actions"][0].expressions[0], exp.Tuple)
        with self.assertRaises(UnsupportedError):
            selector.sql("doris", unsupported_level=ErrorLevel.RAISE)
        self.assertEqual(
            self.parse_one('ALTER TABLE "raw"."Fact" DROP PARTITION "set"').sql(
                "doris"
            ),
            "ALTER TABLE `raw`.`Fact` DROP PARTITION `set`",
        )

    def test_multiple_partition_actions(self):
        for source, target in (
            (
                "ALTER TABLE fact ADD PARTITION p1 START (1) END (2), ADD PARTITION p2 START (2) END (3)",
                "ALTER TABLE fact ADD PARTITION p1 VALUES [('1'), ('2')), ADD PARTITION p2 VALUES [('2'), ('3'))",
            ),
            (
                "ALTER TABLE fact DROP PARTITION IF EXISTS p1, DROP PARTITION p2",
                "ALTER TABLE fact DROP PARTITION IF EXISTS p1, DROP PARTITION p2",
            ),
        ):
            tree = self.parse_one(source)
            self.assertEqual(len(tree.args["actions"]), 2)
            self.assertEqual(
                tree.sql("doris", unsupported_level=ErrorLevel.RAISE), target
            )
            self.assertEqual(parse_one(target, read="doris").sql("doris"), target)

    def test_missing_range_clauses(self):
        for sql in (
            "ALTER TABLE fact ADD PARTITION IF NOT EXISTS p1",
            "ALTER TABLE fact ADD PARTITION p1 START (1)",
            "ALTER TABLE fact ADD PARTITION p1 START (1) END",
            "ALTER TABLE fact DROP PARTITION",
        ):
            with self.assertRaises(ParseError):
                parse_one(sql, read="adb", error_level=ErrorLevel.IMMEDIATE)

    def test_extra_partition_options_retained(self):
        source = (
            "ALTER TABLE fact ADD PARTITION p1 START (1) END (2) WITH (appendonly=true)"
        )
        tree = self.parse_one(source)
        self.assertTrue(tree.args["actions"][0].args.get("properties"))
        self.assertIn("appendonly", tree.sql("adb"))

    def test_unnamed_range_retains_bounds_without_guessing_name(self):
        tree = self.validate_identity(
            "ALTER TABLE fact ADD PARTITION START (1) INCLUSIVE END (2) EXCLUSIVE"
        )
        self.assertIsNone(tree.args["actions"][0].this)
        with self.assertRaises(UnsupportedError):
            tree.sql("doris", unsupported_level=ErrorLevel.RAISE)

    def test_static_date_bounds(self):
        for text in ("2026-10-14", "20261014"):
            tree = self.parse_one(
                f"ALTER TABLE fact ADD PARTITION p1 START (DATE '{text}') END (DATE '2026-10-15')"
            )
            self.assertIsInstance(
                tree.args["actions"][0].expression.args["from_expressions"][0], exp.Cast
            )
            target = (
                "ALTER TABLE fact ADD PARTITION p1 VALUES [('20261014'), ('20261015'))"
            )
            self.assertEqual(
                tree.sql("doris", unsupported_level=ErrorLevel.RAISE), target
            )
        for value in (
            "DATE '2026-02-30'",
            "DATE '10/01/2026'",
            "DATE 'today'",
            "DATE '2026-1014'",
            "CAST('2026-10-14' AS TIMESTAMP)",
            "CAST('2026-10-14' AS TIMESTAMPTZ)",
            "CAST(x AS DATE)",
        ):
            with self.subTest(value=value):
                tree = self.parse_one(
                    f"ALTER TABLE fact ADD PARTITION p1 START ({value}) END (DATE '2026-10-15')"
                )
                with self.assertRaises(UnsupportedError):
                    tree.sql("doris", unsupported_level=ErrorLevel.RAISE)

    def test_postgres_remains_unchanged(self):
        tree = parse_one("ALTER TABLE fact ADD COLUMN id INT", read="postgres")
        self.assertIsInstance(tree.args["actions"][0], exp.ColumnDef)
        self.assertEqual(tree.sql("postgres"), "ALTER TABLE fact ADD COLUMN id INT")

    def test_doris_numeric_bounds_are_integer_literals(self):
        for value in ("1.25", "-1.25", "1.0", "1e3", "-1e3", "1E-3"):
            for clause in ("START ({}) END (2000)", "START (-2000) END ({})"):
                with self.subTest(value=value, clause=clause):
                    tree = self.parse_one(
                        "ALTER TABLE fact ADD PARTITION p1 " + clause.format(value)
                    )
                    with self.assertRaises(UnsupportedError):
                        tree.sql("doris", unsupported_level=ErrorLevel.RAISE)
        tree = self.parse_one(
            "ALTER TABLE fact ADD PARTITION p1 START (-1, '1.25') END (2, '2.50')"
        )
        self.assertEqual(
            tree.sql("doris", unsupported_level=ErrorLevel.RAISE),
            "ALTER TABLE fact ADD PARTITION p1 VALUES [('-1', '1.25'), ('2', '2.50'))",
        )

    def test_integer_bounds_use_exact_decimal_strings(self):
        for lower, upper in (
            ("-2", "-1"),
            ("0", "1"),
            ("-9223372036854775808", "9223372036854775808"),
            (
                "-170141183460469231731687303715884105728",
                "170141183460469231731687303715884105727",
            ),
        ):
            with self.subTest(lower=lower, upper=upper):
                tree = self.parse_one(
                    f"ALTER TABLE fact ADD PARTITION p1 START ({lower}) END ({upper})"
                )
                source_tree = tree.copy()
                target = f"ALTER TABLE fact ADD PARTITION p1 VALUES [('{lower}'), ('{upper}'))"
                self.assertEqual(
                    tree.sql("doris", unsupported_level=ErrorLevel.RAISE), target
                )
                self.assertEqual(tree, source_tree)
                self.assertEqual(parse_one(target, read="doris").sql("doris"), target)
        tree = self.parse_one(
            "ALTER TABLE fact ADD PARTITION p1 START (-00, 0001) END (02, 03)"
        )
        self.assertEqual(
            tree.sql("doris", unsupported_level=ErrorLevel.RAISE),
            "ALTER TABLE fact ADD PARTITION p1 VALUES [('0', '1'), ('2', '3'))",
        )
        tree = self.parse_one(
            "ALTER TABLE fact ADD PARTITION p1 START ('-00', '0001') END ('02', '03')"
        )
        self.assertEqual(
            tree.sql("doris", unsupported_level=ErrorLevel.RAISE),
            "ALTER TABLE fact ADD PARTITION p1 VALUES [('-00', '0001'), ('02', '03'))",
        )

    def test_doris_range_bound_tuple_arity_is_checked(self):
        for lower, upper in (([], []), ([1], []), ([], [2]), ([1, 2], [3])):
            with self.subTest(lower=lower, upper=upper):
                tree = self.parse_one(
                    "ALTER TABLE fact ADD PARTITION p1 START (1) END (2)"
                )
                bound = tree.args["actions"][0].expression
                bound.set("from_expressions", [exp.Literal.number(x) for x in lower])
                bound.set("to_expressions", [exp.Literal.number(x) for x in upper])
                with self.assertRaises(UnsupportedError):
                    tree.sql("doris", unsupported_level=ErrorLevel.RAISE)

    def test_doris_does_not_support_minvalue(self):
        tree = self.parse_one("ALTER TABLE fact ADD PARTITION p1 START (1) END (2)")
        bound = tree.args["actions"][0].expression
        bound.set("from_expressions", [exp.Var(this="MINVALUE")])
        with self.assertRaises(UnsupportedError):
            tree.sql("doris", unsupported_level=ErrorLevel.RAISE)
        bound.set("from_expressions", [exp.Literal.number(1)])
        bound.set("to_expressions", [exp.Var(this="MAXVALUE")])
        self.assertEqual(
            tree.sql("doris", unsupported_level=ErrorLevel.RAISE),
            "ALTER TABLE fact ADD PARTITION p1 VALUES [('1'), (MAXVALUE))",
        )
