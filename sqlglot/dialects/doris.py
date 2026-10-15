from __future__ import annotations

from datetime import date
import re

from sqlglot import exp, transforms
from sqlglot.dialects.dialect import (
    approx_count_distinct_sql,
    build_timestamp_trunc,
    rename_func,
    property_sql,
    time_format,
    unit_to_str,
)
from sqlglot.dialects.mysql import MySQL
from sqlglot.tokens import TokenType


def _lag_lead_sql(self, expression: exp.Lag | exp.Lead) -> str:
    return self.func(
        "LAG" if isinstance(expression, exp.Lag) else "LEAD",
        expression.this,
        expression.args.get("offset") or exp.Literal.number(1),
        expression.args.get("default") or exp.null(),
    )


def _build_date_trunc(args: list[exp.Expression]) -> exp.TimestampTrunc:
    # Doris accepts both argument orders. Only a literal time unit identifies
    # the unit-first form; datetime columns and expressions must stay in this.
    first = args[0] if args else None
    if (
        isinstance(first, exp.Literal)
        and first.is_string
        and first.this.upper()
        in {"SECOND", "MINUTE", "HOUR", "DAY", "WEEK", "MONTH", "QUARTER", "YEAR"}
    ):
        return build_timestamp_trunc(args)
    return exp.TimestampTrunc.from_arg_list(args)


class Doris(MySQL):
    DATE_FORMAT = "'yyyy-MM-dd'"
    DATEINT_FORMAT = "'yyyyMMdd'"
    TIME_FORMAT = "'yyyy-MM-dd HH:mm:ss'"

    class Parser(MySQL.Parser):
        def _parse_truncate_table(self):
            tree = super()._parse_truncate_table()
            if isinstance(tree, exp.TruncateTable):
                if len(tree.expressions) == 1 and isinstance(
                    tree.expressions[0], exp.Table
                ):
                    table = tree.expressions[0]
                    if table.args.get("partition") is not None:
                        tree.set("partition", table.args.pop("partition"))
                partition = tree.args.get("partition")
                if isinstance(partition, exp.Partition):
                    partition.set(
                        "expressions",
                        [
                            value.this
                            if isinstance(value, exp.Column) and len(value.parts) == 1
                            else value
                            for value in partition.expressions
                        ],
                    )
            return tree
        PROPERTY_PARSERS = {
            **MySQL.Parser.PROPERTY_PARSERS,
            "PROPERTIES": lambda self: self._parse_wrapped_properties(),
        }

        FUNCTIONS = {
            **MySQL.Parser.FUNCTIONS,
            "COLLECT_SET": exp.ArrayUniqueAgg.from_arg_list,
            "DATE_TRUNC": _build_date_trunc,
            "MONTHS_ADD": exp.AddMonths.from_arg_list,
            "REGEXP": exp.RegexpLike.from_arg_list,
            "TO_DATE": exp.TsOrDsToDate.from_arg_list,
        }

        FUNCTION_PARSERS = MySQL.Parser.FUNCTION_PARSERS.copy()
        FUNCTION_PARSERS.pop("GROUP_CONCAT")

        def _parse_alter_table_add(self):
            if not self._match(TokenType.PARTITION, advance=False):
                return super()._parse_alter_table_add()
            index = self._index
            actions = self._parse_csv(self._parse_add_range_partition)
            if actions:
                return actions
            self._retreat(index)
            return super()._parse_alter_table_add()

        def _parse_add_range_partition(self):
            index = self._index
            self._match_text_seq("ADD")
            if not self._match(TokenType.PARTITION):
                if self._curr is None:
                    self.raise_error("Expecting PARTITION")
                self._retreat(index)
                return None
            exists = self._parse_exists(not_=True)
            name = self._parse_id_var(any_token=False)
            if name is None or not self._match_pair(
                TokenType.VALUES, TokenType.L_BRACKET
            ):
                self._retreat(index)
                return None
            lower = self._parse_wrapped_csv(self._parse_assignment)
            if not self._match(TokenType.COMMA):
                self.raise_error("Expecting comma between partition bounds")
            upper = self._parse_wrapped_csv(self._parse_assignment)
            if not self._match(TokenType.R_PAREN):
                self.raise_error("Expecting exclusive upper bound )")
            return self.expression(
                exp.AddPartition,
                this=name,
                exists=exists,
                expression=self.expression(
                    exp.PartitionBoundSpec,
                    from_expressions=lower,
                    to_expressions=upper,
                    from_inclusive=True,
                    to_inclusive=False,
                ),
            )

        def _parse_alter_table_drop(self):
            if not self._match(TokenType.PARTITION, advance=False):
                return super()._parse_alter_table_drop()
            return self._parse_csv(self._parse_named_drop_partition)

        def _parse_named_drop_partition(self):
            self._match(TokenType.DROP)
            if not self._match(TokenType.PARTITION):
                self.raise_error("Expecting PARTITION")
            exists = self._parse_exists()
            name = self._parse_id_var(any_token=False)
            if name is None:
                self.raise_error("Expecting partition name")
            partition = self.expression(exp.Partition, expressions=[name])
            return self.expression(
                exp.DropPartition, expressions=[partition], exists=exists
            )

    class Generator(MySQL.Generator):
        WITH_PROPERTIES_PREFIX = "PROPERTIES"

        def truncatetable_sql(self, expression):
            partition = expression.args.get("partition")
            if (
                any(
                    expression.args.get(key)
                    for key in (
                        "is_database",
                        "exists",
                        "only",
                        "cluster",
                        "identity",
                        "option",
                    )
                )
                or len(expression.expressions) != 1
            ):
                self.unsupported(
                    "Doris TRUNCATE requires one table without source options"
                )
            if partition is not None and not (
                isinstance(partition, exp.Partition)
                and not partition.args.get("subpartition")
                and partition.expressions
                and all(isinstance(name, exp.Identifier) for name in partition.expressions)
            ):
                self.unsupported(
                    "Doris partition TRUNCATE requires explicit partition names"
                )
                return ""
            return super().truncatetable_sql(expression)

        def addpartition_sql(self, expression):
            if not isinstance(expression.this, exp.Identifier):
                self.unsupported(
                    "Doris ADD PARTITION requires an explicit partition name"
                )
            if expression.args.get("properties"):
                self.unsupported(
                    "Partition storage properties require an explicit Doris mapping"
                )
            return super().addpartition_sql(expression)

        def partitionboundspec_sql(self, expression):
            if (
                expression.this
                or expression.expression
                or expression.args.get("from_inclusive") is False
                or expression.args.get("to_inclusive")
            ):
                self.unsupported(
                    "Doris range partitions require inclusive lower / exclusive upper bounds"
                )
            lower = expression.args.get("from_expressions") or []
            upper = expression.args.get("to_expressions") or []
            if not lower or len(lower) != len(upper):
                self.unsupported(
                    "Doris range partition bounds must be nonempty tuples of equal length"
                )
            lower = self._partition_bound_values(lower)
            upper = self._partition_bound_values(upper)
            return f"VALUES [({lower}), ({upper}))"

        def _partition_bound_values(self, expressions):
            values = []
            for value in expressions:
                if (
                    type(value) is exp.Cast
                    and isinstance(value.this, exp.Literal)
                    and value.this.is_string
                    and isinstance(value.to, exp.DataType)
                    and value.to.this == exp.DataType.Type.DATE
                    and not any(
                        value.args.get(key)
                        for key in ("format", "safe", "action", "default")
                    )
                ):
                    text = value.this.this
                    match = re.fullmatch(
                        r"(?:[0-9]{8}|[0-9]{4}-[0-9]{2}-[0-9]{2})", text
                    )
                    digits = text.replace("-", "")
                    try:
                        parsed = (
                            date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
                            if match
                            else None
                        )
                    except ValueError:
                        parsed = None
                    if parsed is not None:
                        value = exp.Literal.string(
                            f"{parsed.year:04d}{parsed.month:02d}{parsed.day:02d}"
                        )
                number = value.this if isinstance(value, exp.Neg) else value
                if (
                    isinstance(number, exp.Literal)
                    and number.is_number
                    and re.fullmatch(r"[0-9]+", number.this)
                ):
                    digits = number.this.lstrip("0") or "0"
                    if isinstance(value, exp.Neg) and digits != "0":
                        digits = "-" + digits
                    value = exp.Literal.string(digits)
                if not (
                    (isinstance(value, exp.Literal) and value.is_string)
                    or (isinstance(value, exp.Var) and value.name.upper() == "MAXVALUE")
                ):
                    self.unsupported(
                        "Doris partition bounds require string literals, integers or MAXVALUE"
                    )
                values.append(self.sql(value))
            return ", ".join(values)

        def droppartition_sql(self, expression):
            clauses = []
            exists = " IF EXISTS" if expression.args.get("exists") else ""
            for partition in expression.expressions:
                if not (
                    isinstance(partition, exp.Partition)
                    and not partition.args.get("subpartition")
                    and len(partition.expressions) == 1
                    and isinstance(partition.expressions[0], exp.Identifier)
                ):
                    self.unsupported(
                        "Doris DROP PARTITION requires an explicit partition name"
                    )
                    continue
                clauses.append(
                    f"DROP PARTITION{exists} {self.sql(partition.expressions[0])}"
                )
            return ", ".join(clauses)

        LAST_DAY_SUPPORTS_DATE_PART = False
        VARCHAR_REQUIRES_SIZE = False

        TYPE_MAPPING = {
            **MySQL.Generator.TYPE_MAPPING,
            exp.DataType.Type.TEXT: "STRING",
            exp.DataType.Type.TIMESTAMP: "DATETIME",
            exp.DataType.Type.TIMESTAMPTZ: "DATETIME",
        }

        CAST_MAPPING = {}
        TIMESTAMP_FUNC_TYPES = set()

        TRANSFORMS = {
            **MySQL.Generator.TRANSFORMS,
            exp.Property: property_sql,
            exp.AddMonths: rename_func("MONTHS_ADD"),
            exp.ApproxDistinct: approx_count_distinct_sql,
            exp.ArgMax: rename_func("MAX_BY"),
            exp.ArgMin: rename_func("MIN_BY"),
            exp.ArrayAgg: rename_func("COLLECT_LIST"),
            exp.ArrayUniqueAgg: rename_func("COLLECT_SET"),
            exp.CurrentTimestamp: lambda self, _: self.func("NOW"),
            exp.DateTrunc: lambda self, e: self.func("DATE_TRUNC", e.this, unit_to_str(e)),
            exp.GroupConcat: lambda self, e: self.func(
                "GROUP_CONCAT", e.this, e.args.get("separator") or exp.Literal.string(",")
            ),
            exp.JSONExtractScalar: lambda self, e: self.func("JSON_EXTRACT", e.this, e.expression),
            exp.Lag: _lag_lead_sql,
            exp.Lead: _lag_lead_sql,
            exp.Map: rename_func("ARRAY_MAP"),
            exp.RegexpLike: rename_func("REGEXP"),
            exp.RegexpSplit: rename_func("SPLIT_BY_STRING"),
            exp.Select: transforms.preprocess(
                [
                    transforms.eliminate_distinct_on,
                    transforms.eliminate_semi_and_anti_joins,
                    transforms.eliminate_qualify,
                    transforms.unnest_generate_date_array_using_recursive_cte,
                ]
            ),
            exp.Split: rename_func("SPLIT_BY_STRING"),
            exp.StringToArray: rename_func("SPLIT_BY_STRING"),
            exp.StrToUnix: lambda self, e: self.func("UNIX_TIMESTAMP", e.this, self.format_time(e)),
            exp.TimeStrToDate: rename_func("TO_DATE"),
            exp.TsOrDsAdd: lambda self, e: self.func("DATE_ADD", e.this, e.expression),
            exp.TsOrDsToDate: lambda self, e: self.func("TO_DATE", e.this),
            exp.TimeToUnix: rename_func("UNIX_TIMESTAMP"),
            exp.TimestampTrunc: lambda self, e: self.func("DATE_TRUNC", e.this, unit_to_str(e)),
            exp.UnixToStr: lambda self, e: self.func(
                "FROM_UNIXTIME", e.this, time_format("doris")(self, e)
            ),
            exp.UnixToTime: rename_func("FROM_UNIXTIME"),
        }

        # https://github.com/apache/doris/blob/e4f41dbf1ec03f5937fdeba2ee1454a20254015b/fe/fe-core/src/main/antlr4/org/apache/doris/nereids/DorisLexer.g4#L93
        RESERVED_KEYWORDS = {
            "account_lock",
            "account_unlock",
            "add",
            "adddate",
            "admin",
            "after",
            "agg_state",
            "aggregate",
            "alias",
            "all",
            "alter",
            "analyze",
            "analyzed",
            "and",
            "anti",
            "append",
            "array",
            "array_range",
            "as",
            "asc",
            "at",
            "authors",
            "auto",
            "auto_increment",
            "backend",
            "backends",
            "backup",
            "begin",
            "belong",
            "between",
            "bigint",
            "bin",
            "binary",
            "binlog",
            "bitand",
            "bitmap",
            "bitmap_union",
            "bitor",
            "bitxor",
            "blob",
            "boolean",
            "brief",
            "broker",
            "buckets",
            "build",
            "builtin",
            "bulk",
            "by",
            "cached",
            "call",
            "cancel",
            "case",
            "cast",
            "catalog",
            "catalogs",
            "chain",
            "char",
            "character",
            "charset",
            "check",
            "clean",
            "cluster",
            "clusters",
            "collate",
            "collation",
            "collect",
            "column",
            "columns",
            "comment",
            "commit",
            "committed",
            "compact",
            "complete",
            "config",
            "connection",
            "connection_id",
            "consistent",
            "constraint",
            "constraints",
            "convert",
            "copy",
            "count",
            "create",
            "creation",
            "cron",
            "cross",
            "cube",
            "current",
            "current_catalog",
            "current_date",
            "current_time",
            "current_timestamp",
            "current_user",
            "data",
            "database",
            "databases",
            "date",
            "date_add",
            "date_ceil",
            "date_diff",
            "date_floor",
            "date_sub",
            "dateadd",
            "datediff",
            "datetime",
            "datetimev2",
            "datev2",
            "datetimev1",
            "datev1",
            "day",
            "days_add",
            "days_sub",
            "decimal",
            "decimalv2",
            "decimalv3",
            "decommission",
            "default",
            "deferred",
            "delete",
            "demand",
            "desc",
            "describe",
            "diagnose",
            "disk",
            "distinct",
            "distinctpc",
            "distinctpcsa",
            "distributed",
            "distribution",
            "div",
            "do",
            "doris_internal_table_id",
            "double",
            "drop",
            "dropp",
            "dual",
            "duplicate",
            "dynamic",
            "else",
            "enable",
            "encryptkey",
            "encryptkeys",
            "end",
            "ends",
            "engine",
            "engines",
            "enter",
            "errors",
            "events",
            "every",
            "except",
            "exclude",
            "execute",
            "exists",
            "expired",
            "explain",
            "export",
            "extended",
            "external",
            "extract",
            "failed_login_attempts",
            "false",
            "fast",
            "feature",
            "fields",
            "file",
            "filter",
            "first",
            "float",
            "follower",
            "following",
            "for",
            "foreign",
            "force",
            "format",
            "free",
            "from",
            "frontend",
            "frontends",
            "full",
            "function",
            "functions",
            "generic",
            "global",
            "grant",
            "grants",
            "graph",
            "group",
            "grouping",
            "groups",
            "hash",
            "having",
            "hdfs",
            "help",
            "histogram",
            "hll",
            "hll_union",
            "hostname",
            "hour",
            "hub",
            "identified",
            "if",
            "ignore",
            "immediate",
            "in",
            "incremental",
            "index",
            "indexes",
            "infile",
            "inner",
            "insert",
            "install",
            "int",
            "integer",
            "intermediate",
            "intersect",
            "interval",
            "into",
            "inverted",
            "ipv4",
            "ipv6",
            "is",
            "is_not_null_pred",
            "is_null_pred",
            "isnull",
            "isolation",
            "job",
            "jobs",
            "join",
            "json",
            "jsonb",
            "key",
            "keys",
            "kill",
            "label",
            "largeint",
            "last",
            "lateral",
            "ldap",
            "ldap_admin_password",
            "left",
            "less",
            "level",
            "like",
            "limit",
            "lines",
            "link",
            "list",
            "load",
            "local",
            "localtime",
            "localtimestamp",
            "location",
            "lock",
            "logical",
            "low_priority",
            "manual",
            "map",
            "match",
            "match_all",
            "match_any",
            "match_phrase",
            "match_phrase_edge",
            "match_phrase_prefix",
            "match_regexp",
            "materialized",
            "max",
            "maxvalue",
            "memo",
            "merge",
            "migrate",
            "migrations",
            "min",
            "minus",
            "minute",
            "modify",
            "month",
            "mtmv",
            "name",
            "names",
            "natural",
            "negative",
            "never",
            "next",
            "ngram_bf",
            "no",
            "non_nullable",
            "not",
            "null",
            "nulls",
            "observer",
            "of",
            "offset",
            "on",
            "only",
            "open",
            "optimized",
            "or",
            "order",
            "outer",
            "outfile",
            "over",
            "overwrite",
            "parameter",
            "parsed",
            "partition",
            "partitions",
            "password",
            "password_expire",
            "password_history",
            "password_lock_time",
            "password_reuse",
            "path",
            "pause",
            "percent",
            "period",
            "permissive",
            "physical",
            "plan",
            "process",
            "plugin",
            "plugins",
            "policy",
            "preceding",
            "prepare",
            "primary",
            "proc",
            "procedure",
            "processlist",
            "profile",
            "properties",
            "property",
            "quantile_state",
            "quantile_union",
            "query",
            "quota",
            "random",
            "range",
            "read",
            "real",
            "rebalance",
            "recover",
            "recycle",
            "refresh",
            "references",
            "regexp",
            "release",
            "rename",
            "repair",
            "repeatable",
            "replace",
            "replace_if_not_null",
            "replica",
            "repositories",
            "repository",
            "resource",
            "resources",
            "restore",
            "restrictive",
            "resume",
            "returns",
            "revoke",
            "rewritten",
            "right",
            "rlike",
            "role",
            "roles",
            "rollback",
            "rollup",
            "routine",
            "row",
            "rows",
            "s3",
            "sample",
            "schedule",
            "scheduler",
            "schema",
            "schemas",
            "second",
            "select",
            "semi",
            "sequence",
            "serializable",
            "session",
            "set",
            "sets",
            "shape",
            "show",
            "signed",
            "skew",
            "smallint",
            "snapshot",
            "soname",
            "split",
            "sql_block_rule",
            "start",
            "starts",
            "stats",
            "status",
            "stop",
            "storage",
            "stream",
            "streaming",
            "string",
            "struct",
            "subdate",
            "sum",
            "superuser",
            "switch",
            "sync",
            "system",
            "table",
            "tables",
            "tablesample",
            "tablet",
            "tablets",
            "task",
            "tasks",
            "temporary",
            "terminated",
            "text",
            "than",
            "then",
            "time",
            "timestamp",
            "timestampadd",
            "timestampdiff",
            "tinyint",
            "to",
            "transaction",
            "trash",
            "tree",
            "triggers",
            "trim",
            "true",
            "truncate",
            "type",
            "type_cast",
            "types",
            "unbounded",
            "uncommitted",
            "uninstall",
            "union",
            "unique",
            "unlock",
            "unsigned",
            "update",
            "use",
            "user",
            "using",
            "value",
            "values",
            "varchar",
            "variables",
            "variant",
            "vault",
            "verbose",
            "version",
            "view",
            "warnings",
            "week",
            "when",
            "where",
            "whitelist",
            "with",
            "work",
            "workload",
            "write",
            "xor",
            "year",
        }
