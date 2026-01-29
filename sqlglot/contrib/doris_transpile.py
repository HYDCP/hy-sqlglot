"""
Doris Dialect Transpilation Utility

Provides enhanced functionality for transpiling from other dialects (especially PostgreSQL) to Doris,
solving identifier case sensitivity differences.

Usage:
    # Method 1: Direct replacement of transpile
    from sqlglot.contrib.doris_transpile import transpile_to_doris as transpile
    result = transpile("SELECT T.id FROM test t", read="postgres")
    
    # Method 2: Use original signature
    from sqlglot.contrib.doris_transpile import transpile_to_doris
    result = transpile_to_doris("SELECT T.id FROM test t", read="postgres", write="doris")
    
    # Method 3: Handle UNNEST/EXPLODE auto-conversion to LATERAL VIEW
    from sqlglot.contrib.doris_transpile import pg_to_doris
    result = pg_to_doris("SELECT id, unnest(string_to_array(tags, ',')) AS tag FROM t")
    # => ['SELECT id, _explode_tmp.tag FROM t LATERAL VIEW EXPLODE(SPLIT_BY_STRING(tags, ',')) _explode_tmp AS tag']
"""

from __future__ import annotations

import typing as t

from sqlglot import exp, parse
from sqlglot.dialects.dialect import Dialect, NormalizationStrategy
from sqlglot.errors import ErrorLevel

if t.TYPE_CHECKING:
    from sqlglot.dialects.dialect import DialectType

# Counter for auto-generating aliases
_explode_counter = 0


class IdentifierNormalizeMode:
    """Identifier normalization modes"""
    NONE = "none"                    # No normalization
    # Normalize all identifiers (including column names)
    ALL = "all"
    TABLE_ONLY = "table_only"        # Only normalize table names
    ALIAS_ONLY = "alias_only"        # Only normalize table aliases
    TABLE_AND_ALIAS = "table_and_alias"  # Normalize table names and aliases
    # Normalize table aliases + table references in columns
    TABLE_REFS = "table_refs"
    # Normalize table names + aliases + table refs in columns (recommended)
    TABLE_FULL = "table_full"


def _generate_explode_alias() -> str:
    """Generate a unique EXPLODE table alias"""
    global _explode_counter
    _explode_counter += 1
    return f"_explode_tmp_{_explode_counter}"


def explode_to_lateral_view(expression: exp.Expression) -> exp.Expression:
    """
    Convert EXPLODE/UNNEST in SELECT list to LATERAL VIEW syntax.

    In PostgreSQL, unnest() can be used directly in SELECT, but Doris requires LATERAL VIEW:

    Before (PostgreSQL):
        SELECT id, unnest(string_to_array(tags, ',')) AS tag FROM t

    After (Doris):
        SELECT id, _explode_tmp.tag FROM t LATERAL VIEW EXPLODE(SPLIT_BY_STRING(tags, ',')) _explode_tmp AS tag

    Args:
        expression: The AST to process

    Returns:
        The processed AST
    """
    # Recursively process all SELECT statements (including subqueries)
    for select in expression.find_all(exp.Select):
        _transform_select_explode(select)

    return expression


def _transform_select_explode(select: exp.Select) -> None:
    """
    Transform EXPLODE in a single SELECT statement.

    Args:
        select: The SELECT node
    """
    new_expressions = []
    laterals_to_add = []

    for expr in select.expressions:
        explode_node = None
        alias_name = None

        # Case 1: Alias(Explode(...), alias=xxx)
        if isinstance(expr, exp.Alias):
            inner = expr.this
            if isinstance(inner, (exp.Explode, exp.Posexplode, exp.Unnest)):
                explode_node = inner
                alias_name = expr.alias

        # Case 2: Direct Explode(...) without alias
        elif isinstance(expr, (exp.Explode, exp.Posexplode, exp.Unnest)):
            explode_node = expr
            alias_name = None

        if explode_node is not None:
            # Need an alias to reference the exploded column
            if not alias_name:
                alias_name = "_col"

            # Generate table alias
            table_alias_name = _generate_explode_alias()

            # Create LATERAL VIEW node
            lateral = exp.Lateral(
                this=explode_node.copy(),
                view=True,
                alias=exp.TableAlias(
                    this=exp.to_identifier(table_alias_name),
                    columns=[exp.to_identifier(alias_name)]
                )
            )
            laterals_to_add.append(lateral)

            # Replace EXPLODE with column reference in SELECT (include table alias)
            new_expressions.append(exp.Column(
                this=exp.to_identifier(alias_name),
                table=exp.to_identifier(table_alias_name)))
        else:
            new_expressions.append(expr)

    # Apply modifications
    if laterals_to_add:
        select.set("expressions", new_expressions)

        # Add LATERAL VIEW to SELECT
        existing_laterals = select.args.get("laterals") or []
        select.set("laterals", existing_laterals + laterals_to_add)


def add_alias_to_cast(expression: exp.Expression) -> exp.Expression:
    """
    Automatically add column name as alias for simple CAST(column AS type) expressions without alias.

    Rules:
        - CAST(col AS type)      -> CAST(col AS type) AS col  (add alias)
        - CAST(col AS type) AS x -> skip (already has alias)
        - CAST(col + 1 AS type)  -> skip (not a simple column reference)
        - CAST(func(col) AS type)-> skip (not a simple column reference)

    Args:
        expression: The AST to process

    Returns:
        The processed AST
    """
    # Iterate over all SELECT statements
    for select in expression.find_all(exp.Select):
        new_expressions = []
        modified = False

        for expr in select.expressions:
            # Check if it's a Cast without alias
            if isinstance(expr, exp.Cast):
                # Check if the Cast content is a simple Column
                cast_this = expr.args.get("this")
                if isinstance(cast_this, exp.Column):
                    # Get column name
                    col_name = cast_this.name
                    if col_name:
                        # Wrap Cast with Alias
                        aliased = exp.Alias(
                            this=expr,
                            alias=exp.to_identifier(col_name)
                        )
                        new_expressions.append(aliased)
                        modified = True
                        continue

            new_expressions.append(expr)

        # If modified, update SELECT expressions
        if modified:
            select.set("expressions", new_expressions)

    return expression


def normalize_table_identifiers(
    expression: exp.Expression,
    source_dialect: DialectType = None,
    mode: str = IdentifierNormalizeMode.TABLE_FULL,
) -> exp.Expression:
    """
    Normalize table-related identifiers.

    Args:
        expression: The AST to process
        source_dialect: Source dialect (used to determine normalization strategy)
        mode: Normalization mode
            - "none": No normalization
            - "all": Normalize all identifiers (including column names)
            - "table_only": Only normalize table names
            - "alias_only": Only normalize table aliases
            - "table_and_alias": Normalize table names and aliases
            - "table_refs": Normalize table aliases + unify table references in columns
            - "table_full": Normalize table names + aliases + table refs in columns (recommended, default)

    Returns:
        The normalized AST
    """
    if mode == IdentifierNormalizeMode.NONE:
        return expression

    if mode == IdentifierNormalizeMode.ALL:
        from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
        return normalize_identifiers(expression, dialect=source_dialect)

    # Determine normalization function
    dialect = Dialect.get_or_raise(source_dialect)
    strategy = dialect.NORMALIZATION_STRATEGY

    if strategy == NormalizationStrategy.UPPERCASE:
        normalize_func = str.upper
    elif strategy == NormalizationStrategy.CASE_SENSITIVE:
        def normalize_func(x): return x  # No normalization
    else:
        # LOWERCASE, CASE_INSENSITIVE, CASE_INSENSITIVE_UPPERCASE default to lowercase
        normalize_func = str.lower

    # Collect table name/alias mappings (for unifying table references)
    alias_mapping: t.Dict[str, str] = {}

    def _normalize_table_alias(alias: exp.TableAlias) -> None:
        """Normalize TableAlias and record mapping"""
        alias_id = alias.args.get("this")
        if isinstance(alias_id, exp.Identifier) and not alias_id.quoted:
            original = alias_id.this
            normalized = normalize_func(original)
            # 记录映射
            alias_mapping[original] = normalized
            alias_mapping[original.lower()] = normalized
            alias_mapping[original.upper()] = normalized
            # Update alias
            alias_id.set("this", normalized)

    # Process table nodes
    for table in expression.find_all(exp.Table):
        table_name = table.args.get("this")
        alias = table.args.get("alias")

        # Normalize table name
        if mode in (IdentifierNormalizeMode.TABLE_ONLY,
                    IdentifierNormalizeMode.TABLE_AND_ALIAS,
                    IdentifierNormalizeMode.TABLE_FULL):
            if isinstance(table_name, exp.Identifier) and not table_name.quoted:
                original_table = table_name.this
                normalized_table = normalize_func(original_table)
                table_name.set("this", normalized_table)

                # If no alias, record table name mapping (for column references)
                if not alias and mode in (IdentifierNormalizeMode.TABLE_REFS,
                                          IdentifierNormalizeMode.TABLE_FULL):
                    alias_mapping[original_table] = normalized_table
                    alias_mapping[original_table.lower()] = normalized_table
                    alias_mapping[original_table.upper()] = normalized_table

        # Normalize table alias
        if mode in (IdentifierNormalizeMode.ALIAS_ONLY,
                    IdentifierNormalizeMode.TABLE_AND_ALIAS,
                    IdentifierNormalizeMode.TABLE_REFS,
                    IdentifierNormalizeMode.TABLE_FULL):
            if isinstance(alias, exp.TableAlias):
                _normalize_table_alias(alias)

    # Process subquery aliases (e.g., FROM (SELECT ...) AS t2)
    if mode in (IdentifierNormalizeMode.ALIAS_ONLY,
                IdentifierNormalizeMode.TABLE_AND_ALIAS,
                IdentifierNormalizeMode.TABLE_REFS,
                IdentifierNormalizeMode.TABLE_FULL):
        for subquery in expression.find_all(exp.Subquery):
            alias = subquery.args.get("alias")
            if isinstance(alias, exp.TableAlias):
                _normalize_table_alias(alias)

    # Unify table references in columns
    if mode in (IdentifierNormalizeMode.TABLE_REFS, IdentifierNormalizeMode.TABLE_FULL) and alias_mapping:
        for column in expression.find_all(exp.Column):
            table_ref = column.args.get("table")
            if isinstance(table_ref, exp.Identifier) and not table_ref.quoted:
                original = table_ref.this
                if original in alias_mapping:
                    table_ref.set("this", alias_mapping[original])

    return expression


def transpile_to_doris(
    sql: str,
    read: DialectType = None,
    write: DialectType = "doris",
    identity: bool = True,
    error_level: t.Optional[ErrorLevel] = None,
    normalize_mode: str = IdentifierNormalizeMode.TABLE_FULL,
    auto_alias_cast: bool = True,
    explode_to_lateral: bool = True,
    **opts,
) -> t.List[str]:
    """
    Transpile from source dialect to Doris, automatically handling identifier case issues.

    Signature compatible with sqlglot.transpile(), can be used as a drop-in replacement.

    Args:
        sql: The SQL code to transpile
        read: Source dialect (e.g., "postgres", "spark", "mysql")
        write: Target dialect, defaults to "doris"
        identity: If write is not specified and True, use read as target dialect
        error_level: Parser error level
        normalize_mode: Identifier normalization mode
            - "none": No normalization (original transpile behavior)
            - "all": Normalize all identifiers (including column names)
            - "table_only": Only normalize table names
            - "alias_only": Only normalize table aliases
            - "table_and_alias": Normalize table names and aliases
            - "table_refs": Normalize table aliases + unify table references in columns
            - "table_full": Normalize table names + aliases + table refs in columns (default, recommended)
        auto_alias_cast: Whether to auto-add column name as alias for CAST(col AS type) (default True)
            - CAST(id AS int) -> CAST(id AS int) AS id
            - CAST(id AS int) AS x -> unchanged (already has alias)
            - CAST(id + 1 AS int) -> unchanged (not a simple column)
        explode_to_lateral: Whether to convert EXPLODE/UNNEST in SELECT to LATERAL VIEW (default True)
            - SELECT unnest(arr) AS x -> SELECT tmp.x FROM ... LATERAL VIEW EXPLODE(arr) tmp AS x
            - Doris doesn't support EXPLODE directly in SELECT, must use LATERAL VIEW
        **opts: Other Generator options (e.g., pretty=True)

    Returns:
        List of transpiled SQL statements

    Example:
        >>> from sqlglot.contrib.doris_transpile import transpile_to_doris
        >>>
        >>> # In PostgreSQL, T.id and t.name refer to the same table
        >>> sql = "SELECT T.id, t.name FROM TEST t"
        >>> transpile_to_doris(sql, read="postgres")
        ['SELECT t.id, t.name FROM test AS t']
        >>>
        >>> # Auto-add alias for CAST
        >>> sql = "CREATE TABLE t AS SELECT CAST(id AS int) FROM old_t"
        >>> transpile_to_doris(sql, read="postgres")
        ['CREATE TABLE t AS SELECT CAST(id AS INT) AS id FROM old_t']
        >>>
        >>> # UNNEST auto-converted to LATERAL VIEW
        >>> sql = "SELECT id, unnest(string_to_array(tags, ',')) AS tag FROM t"
        >>> transpile_to_doris(sql, read="postgres")
        ['SELECT id, _explode_tmp.tag FROM t LATERAL VIEW EXPLODE(...) _explode_tmp AS tag']
    """
    write = (read if write is None else write) if identity else write
    write_dialect = Dialect.get_or_raise(write)

    results = []
    for expression in parse(sql, read, error_level=error_level):
        if expression:
            # Apply identifier normalization
            normalized = normalize_table_identifiers(
                expression,
                source_dialect=read,
                mode=normalize_mode
            )

            # Auto-add alias for CAST
            if auto_alias_cast:
                normalized = add_alias_to_cast(normalized)

            # Convert EXPLODE/UNNEST in SELECT to LATERAL VIEW
            if explode_to_lateral:
                normalized = explode_to_lateral_view(normalized)

            results.append(write_dialect.generate(
                normalized, copy=False, **opts))
        else:
            results.append("")

    return results


# Convenience aliases
def pg_to_doris(
    sql: str,
    normalize_mode: str = IdentifierNormalizeMode.TABLE_FULL,
    auto_alias_cast: bool = True,
    explode_to_lateral: bool = True,
    **opts,
) -> t.List[str]:
    """
    Shortcut for PostgreSQL to Doris transpilation.

    Example:
        >>> from sqlglot.contrib.doris_transpile import pg_to_doris
        >>> pg_to_doris("SELECT T.id FROM TEST t")
        ['SELECT t.id FROM test AS t']
        >>> pg_to_doris("SELECT CAST(id AS int) FROM t")
        ['SELECT CAST(id AS INT) AS id FROM t']
        >>> pg_to_doris("SELECT unnest(string_to_array(tags, ',')) AS tag FROM t")
        ['SELECT _explode_tmp.tag FROM t LATERAL VIEW EXPLODE(...) _explode_tmp AS tag']
    """
    return transpile_to_doris(
        sql,
        read="postgres",
        write="doris",
        normalize_mode=normalize_mode,
        auto_alias_cast=auto_alias_cast,
        explode_to_lateral=explode_to_lateral,
        **opts
    )


def spark_to_doris(
    sql: str,
    normalize_mode: str = IdentifierNormalizeMode.TABLE_FULL,
    auto_alias_cast: bool = True,
    explode_to_lateral: bool = True,
    **opts,
) -> t.List[str]:
    """Shortcut for Spark to Doris transpilation."""
    return transpile_to_doris(
        sql,
        read="spark",
        write="doris",
        normalize_mode=normalize_mode,
        auto_alias_cast=auto_alias_cast,
        explode_to_lateral=explode_to_lateral,
        **opts
    )


def hive_to_doris(
    sql: str,
    normalize_mode: str = IdentifierNormalizeMode.TABLE_FULL,
    auto_alias_cast: bool = True,
    explode_to_lateral: bool = True,
    **opts,
) -> t.List[str]:
    """Shortcut for Hive to Doris transpilation."""
    return transpile_to_doris(
        sql,
        read="hive",
        write="doris",
        normalize_mode=normalize_mode,
        auto_alias_cast=auto_alias_cast,
        explode_to_lateral=explode_to_lateral,
        **opts
    )


# Exports
__all__ = [
    "IdentifierNormalizeMode",
    "normalize_table_identifiers",
    "add_alias_to_cast",
    "explode_to_lateral_view",
    "transpile_to_doris",
    "pg_to_doris",
    "spark_to_doris",
    "hive_to_doris",
]
