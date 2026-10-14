from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.postgres import Postgres
from sqlglot.tokens import TokenType


class ADB(Postgres):
    """AnalyticDB for PostgreSQL's distribution and named partition syntax."""

    class Parser(Postgres.Parser):
        def _distribution_clause(self):
            if self._match(TokenType.IDENTIFIER, advance=False):
                return False
            return self._match_text_seq("DISTRIBUTED", "BY", advance=False) or self._match_text_seq(
                "DISTRIBUTED", "RANDOMLY", advance=False
            )

        def _parse_alias(self, this, explicit=False):
            if self._distribution_clause():
                return this
            return super()._parse_alias(this, explicit=explicit)

        def _parse_table_alias(self, alias_tokens=None):
            if self._distribution_clause():
                return None
            return super()._parse_table_alias(alias_tokens=alias_tokens)

        def _parse_distributed_property(self):
            if self._match_text_seq("BY", "(", advance=False):
                self._advance()
                columns = self._parse_wrapped_csv(self._parse_id_var)
                if not columns:
                    self.raise_error("Expecting distribution columns")
                return self.expression(
                    exp.DistributedByProperty, expressions=columns, kind="HASH"
                )
            if self._match_text_seq("RANDOMLY"):
                return self.expression(exp.DistributedByProperty, kind="RANDOM")
            return super()._parse_distributed_property()

        def _parse_alter_table_add(self):
            if not self._match(TokenType.PARTITION, advance=False):
                return super()._parse_alter_table_add()
            return self._parse_csv(self._parse_add_partition)

        def _parse_add_partition(self):
            self._match_text_seq("ADD")
            if not self._match(TokenType.PARTITION):
                self.raise_error("Expecting PARTITION")
            exists = self._parse_exists(not_=True)
            name = (
                None
                if self._match_text_seq("START", "(", advance=False)
                else self._parse_id_var(any_token=False)
            )
            if not self._match_text_seq("START"):
                self.raise_error("Expecting START")
            lower = self._parse_wrapped_csv(self._parse_assignment)
            lower_inclusive = self._parse_bound_inclusion(default=True)
            if not self._match(TokenType.END):
                self.raise_error("Expecting END")
            upper = self._parse_wrapped_csv(self._parse_assignment)
            upper_inclusive = self._parse_bound_inclusion(default=False)
            properties = None
            if self._match(TokenType.WITH):
                properties = self.expression(
                    exp.Properties, expressions=self._parse_wrapped_properties()
                )
            return self.expression(
                exp.AddPartition,
                this=name,
                exists=exists,
                properties=properties,
                expression=self.expression(
                    exp.PartitionBoundSpec,
                    from_expressions=lower,
                    to_expressions=upper,
                    from_inclusive=lower_inclusive,
                    to_inclusive=upper_inclusive,
                ),
            )

        def _parse_bound_inclusion(self, default):
            if self._match_texts(("INCLUSIVE", "EXCLUSIVE")):
                return self._prev.text.upper() == "INCLUSIVE"
            return default

        def _parse_alter_table_drop(self):
            if not self._match(TokenType.PARTITION, advance=False):
                return super()._parse_alter_table_drop()
            return self._parse_csv(self._parse_named_drop_partition)

        def _parse_named_drop_partition(self):
            self._match(TokenType.DROP)
            if not self._match(TokenType.PARTITION):
                self.raise_error("Expecting PARTITION")
            exists = self._parse_exists()
            if self._match(TokenType.FOR):
                selector = self.expression(
                    exp.Tuple,
                    expressions=self._parse_wrapped_csv(self._parse_assignment),
                )
            else:
                name = self._parse_id_var(any_token=False)
                if name is None:
                    self.raise_error("Expecting partition name")
                selector = self.expression(exp.Partition, expressions=[name])
            return self.expression(
                exp.DropPartition, expressions=[selector], exists=exists
            )

    class Generator(Postgres.Generator):
        PROPERTIES_LOCATION = {
            **Postgres.Generator.PROPERTIES_LOCATION,
            exp.DistributedByProperty: exp.Properties.Location.POST_EXPRESSION,
        }

        def distributedbyproperty_sql(self, expression):
            if expression.args.get("buckets") or expression.args.get("order"):
                self.unsupported("ADB distribution does not support BUCKETS or ORDER BY")
            if expression.args.get("kind") == "RANDOM":
                return "DISTRIBUTED RANDOMLY"
            return f"DISTRIBUTED BY ({self.expressions(expression, flat=True)})"

        def partitionboundspec_sql(self, expression):
            if expression.args.get("from_inclusive") is None:
                return super().partitionboundspec_sql(expression)
            lower = self.expressions(expression, key="from_expressions", flat=True)
            upper = self.expressions(expression, key="to_expressions", flat=True)
            lower_kind = (
                "INCLUSIVE" if expression.args["from_inclusive"] else "EXCLUSIVE"
            )
            upper_kind = "INCLUSIVE" if expression.args["to_inclusive"] else "EXCLUSIVE"
            return f"START ({lower}) {lower_kind} END ({upper}) {upper_kind}"

        def droppartition_sql(self, expression):
            clauses = []
            exists = " IF EXISTS" if expression.args.get("exists") else ""
            for partition in expression.expressions:
                if isinstance(partition, exp.Tuple):
                    clauses.append(f"DROP PARTITION{exists} FOR {self.sql(partition)}")
                else:
                    clauses.append(
                        f"DROP PARTITION{exists} {self.expressions(partition, flat=True)}"
                    )
            return ", ".join(clauses)
