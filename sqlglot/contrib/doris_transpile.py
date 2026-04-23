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

import logging
import re
import typing as t

from sqlglot import exp, parse
from sqlglot.dialects.dialect import Dialect, NormalizationStrategy
from sqlglot.dialects.doris import Doris
from sqlglot.dialects.postgres import Postgres

logger = logging.getLogger(__name__)
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


class DorisTranspileGenerator(Doris.Generator):
    """
    Custom Doris Generator that correctly handles PostgreSQL E-strings and
    preserves single-line ``--`` comments containing ``/* ... */`` segments.

    PostgreSQL E-strings (E'...') are parsed by SQLGlot as ByteString nodes.
    The default Doris generator doesn't handle ByteString properly, losing quotes
    and not handling escape sequences correctly.

    This custom generator overrides:
    1. ``bytestring_sql()``: convert ByteString to proper quoted string literals
       with adjusted backslash escaping for regex patterns.
    2. ``maybe_comment()`` / ``connector_sql()``: when a comment body contains
       ``*/`` (typically because the source SQL had ``-- ... /*xxx*/``), emit
       a ``-- ...\\n`` single-line comment instead of wrapping it as
       ``/* ... */``. Doris (like MySQL) does not support nested block
       comments, so the default ``/*...*/`` wrapping would otherwise close
       the outer block at the inner ``*/`` and produce invalid SQL.
       The original ``-`` count (``--``, ``---``, ``----`` ...) is preserved
       byte-for-byte.
    3. ``ordered_sql()``: optionally suppress the ``CASE WHEN x IS NULL ...``
       sort key that sqlglot adds to mimic PostgreSQL's default NULL ordering
       (PG: ASC NULLS LAST / DESC NULLS FIRST; Doris: opposite). Controlled
       by the ``preserve_pg_null_order`` constructor flag (default False ==
       use Doris' native NULL ordering, no rewriting).
    """

    def __init__(self, *args: t.Any, preserve_pg_null_order: bool = False,
                 **kwargs: t.Any) -> None:
        super().__init__(*args, **kwargs)
        self._preserve_pg_null_order = preserve_pg_null_order

    def ordered_sql(self, expression: exp.Ordered) -> str:
        """
        Render an ORDER BY item.

        When ``preserve_pg_null_order`` is False (the default for this
        transpiler), the output uses Doris' native NULL ordering convention
        (ASC NULLS FIRST, DESC NULLS LAST). All PG-derived NULL position
        information is discarded so that:
          * no synthetic ``CASE WHEN x IS NULL THEN 1 ELSE 0 END`` sort key
            is added (which sqlglot upstream emits for unsupported NULL
            ordering);
          * no ``NULLS FIRST`` / ``NULLS LAST`` keyword is emitted either.

        When ``preserve_pg_null_order`` is True, we delegate to the upstream
        implementation so the PG semantics are faithfully preserved via the
        CASE WHEN trick. Use this when downstream queries rely on the exact
        NULL position produced by PostgreSQL.
        """
        if self._preserve_pg_null_order:
            return super().ordered_sql(expression)

        this = self.sql(expression, "this")
        desc = expression.args.get("desc")
        sort_order = " DESC" if desc else (" ASC" if desc is False else "")
        with_fill = self.sql(expression, "with_fill")
        with_fill = f" {with_fill}" if with_fill else ""
        return f"{this}{sort_order}{with_fill}"

    # ------------------------------------------------------------------ #
    # ADB(PostgreSQL) -> Doris specific: preserve "-- ... /*xxx*/"
    # ------------------------------------------------------------------ #

    def _format_doris_comment(self, comment: str) -> str:
        """
        Render a single comment safely for Doris.

        Default behavior: wrap with ``/* ... */`` (same as upstream).

        Special case: if the comment body already contains ``*/``, the source
        was almost certainly a single-line ``-- ... /* ... */`` comment whose
        delimiter was discarded by the tokenizer. Wrapping it in ``/* ... */``
        would create an illegal nested block comment in Doris. We instead
        emit ``-- body\\n`` so the original character form is preserved.

        Surplus leading ``-`` characters (from ``---``, ``----``, ...) are
        kept inside the comment body by sqlglot's tokenizer; we concatenate
        them directly to ``--`` to faithfully reproduce the original prefix
        (e.g. body ``"- foo"`` becomes ``"--- foo"``, never ``"-- - foo"``).
        A single space is inserted only when the body starts with a character
        that would otherwise be glued onto ``--`` and break MySQL/Doris's
        single-line-comment recognition rule (which requires whitespace after
        ``--``).
        """
        if "*/" in comment:
            sep = "" if comment[:1] in ("-", " ", "\t", "\n", "\r") else " "
            return f"--{sep}{comment}\n"
        return f"/*{self.pad_comment(comment)}*/"

    def maybe_comment(
        self,
        sql: str,
        expression: t.Optional[exp.Expression] = None,
        comments: t.Optional[t.List[str]] = None,
        separated: bool = False,
    ) -> str:
        # Mirror upstream Generator.maybe_comment, but route each comment
        # through _format_doris_comment so '*/'-containing bodies fall back
        # to '-- ...\n'.
        comments = (
            ((expression and expression.comments) if comments is None else comments)  # type: ignore
            if self.comments
            else None
        )

        if not comments or isinstance(expression, self.EXCLUDE_COMMENTS):
            return sql

        comments_sql = " ".join(
            self._format_doris_comment(comment) for comment in comments if comment
        )

        if not comments_sql:
            return sql

        comments_sql = self._replace_line_breaks(comments_sql)

        if separated or isinstance(expression, self.WITH_SEPARATED_COMMENTS):
            return (
                f"{self.sep()}{comments_sql}{sql}"
                if not sql or sql[0].isspace()
                else f"{comments_sql}{self.sep()}{sql}"
            )

        return f"{sql} {comments_sql}"

    def connector_sql(
        self,
        expression: exp.Connector,
        op: str,
        stack: t.Optional[t.List[t.Any]] = None,
    ) -> str:
        # Mirror upstream Generator.connector_sql but route the comment
        # attached to AND/OR through _format_doris_comment so '*/' inside a
        # single-line comment does not produce nested block comments.
        if stack is not None:
            if expression.expressions:
                stack.append(self.expressions(expression, sep=f" {op} "))
            else:
                stack.append(expression.right)
                if expression.comments and self.comments:
                    for comment in expression.comments:
                        if comment:
                            op += f" {self._format_doris_comment(comment)}"
                stack.extend((op, expression.left))
            return op

        # For the non-stack path, defer to upstream implementation. Comments
        # attached at the boolean operator level only flow through the
        # stack-based branch above in current sqlglot, so this is fine.
        return super().connector_sql(expression, op, stack)

    def bytestring_sql(self, expression: exp.ByteString) -> str:
        """
        Generate SQL for PostgreSQL E-string (parsed as ByteString).

        PostgreSQL E-strings with double backslashes (E'\\\\s') represent single
        backslashes in the actual string. We need to reduce the escaping level
        by half because Doris's string parser will interpret escape sequences.

        Examples:
            E'/'       → '/'
            E'\\\\s+'  → '\\s+' (Doris parses to \\s+, regex engine receives \\s+)
            E'\\\\w+'  → '\\w+' (Doris parses to \\w+, regex engine receives \\w+)

        Args:
            expression: The ByteString expression node

        Returns:
            Properly quoted and escaped string literal for Doris
        """
        string_value = expression.this

        # Reduce backslash escaping by half
        # ByteString.this contains '\\\\s' (2 backslash chars)
        # We want to output SQL with '\\s' (which needs to be escaped for SQL output)
        if string_value and '\\\\' in string_value:
            string_value = string_value.replace('\\\\', '\\')

        # Manually escape for SQL output
        # Escape single quotes and backslashes
        # SQL standard: double single quotes
        escaped = string_value.replace("'", "''")
        # Escape backslashes for SQL
        escaped = escaped.replace("\\", "\\\\")

        return f"'{escaped}'"


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


def regexp_split_to_table_to_lateral_view(expression: exp.Expression) -> exp.Expression:
    """
    Convert REGEXP_SPLIT_TO_TABLE to LATERAL VIEW EXPLODE(split_by_regexp(...)) syntax.

    In PostgreSQL, REGEXP_SPLIT_TO_TABLE can be used directly in SELECT and can be nested.
    In Doris, it must be converted to LATERAL VIEW with subqueries for nesting.

    Before (PostgreSQL):
        SELECT id, REGEXP_SPLIT_TO_TABLE(REGEXP_SPLIT_TO_TABLE(col, '、'), '，') AS result FROM t

    After (Doris):
        SELECT t2.id, tmp_2.result AS result 
        FROM (
          SELECT t1.id, tmp_1.result AS result 
          FROM (SELECT id, col FROM t) t1 
          LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(t1.col, '、')) tmp_1 AS result
        ) t2 
        LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(t2.result, '，')) tmp_2 AS result

    Args:
        expression: The AST to process

    Returns:
        The processed AST
    """
    # Process all SELECT statements recursively
    for select in list(expression.find_all(exp.Select)):
        _transform_regexp_split_to_table(select)

    return expression


def _transform_regexp_split_to_table(select: exp.Select) -> None:
    """
    Transform REGEXP_SPLIT_TO_TABLE calls in a single SELECT statement.

    This handles nested REGEXP_SPLIT_TO_TABLE by iteratively processing each layer,
    first extracting nested calls into subqueries, then converting the outermost call.

    Args:
        select: The SELECT node to transform
    """
    # Iteratively process until no REGEXP_SPLIT_TO_TABLE remains
    max_iterations = 10  # Prevent infinite loops
    iteration = 0

    while iteration < max_iterations:
        # Find REGEXP_SPLIT_TO_TABLE calls in SELECT expressions
        found = False

        for i, expr in enumerate(select.expressions):
            alias_name = None
            func_call = None

            # Case 1: Alias wrapping the function call
            if isinstance(expr, exp.Alias):
                alias_name = expr.alias
                if isinstance(expr.this, exp.Anonymous) and expr.this.name.upper() == 'REGEXP_SPLIT_TO_TABLE':
                    func_call = expr.this
                    found = True
            # Case 2: Direct function call without alias
            elif isinstance(expr, exp.Anonymous) and expr.name.upper() == 'REGEXP_SPLIT_TO_TABLE':
                func_call = expr
                alias_name = f"_regexp_split_{i}"
                found = True

            if func_call:
                # First, recursively replace nested REGEXP_SPLIT_TO_TABLE in arguments
                func_call = _extract_nested_regexp_split(func_call, select)

                # Now convert this call to LATERAL VIEW
                call_info = {
                    'index': i,
                    'alias': alias_name,
                    'call': func_call,
                    'original_expr': expr
                }
                _wrap_select_with_lateral_view(select, call_info)
                # Process one at a time, then restart
                break

        if not found:
            break

        iteration += 1


def _extract_nested_regexp_split(func_call: exp.Anonymous, select: exp.Select) -> exp.Anonymous:
    """
    Extract nested REGEXP_SPLIT_TO_TABLE from function arguments.

    If a REGEXP_SPLIT_TO_TABLE contains nested REGEXP_SPLIT_TO_TABLE in its arguments,
    this function extracts them to LATERAL VIEWs and replaces them with column references.

    Args:
        func_call: The REGEXP_SPLIT_TO_TABLE function call
        select: The parent SELECT statement

    Returns:
        The function call with nested calls replaced by column references
    """
    # Check each argument for nested REGEXP_SPLIT_TO_TABLE
    new_expressions = []
    laterals_added = []

    for i, arg in enumerate(func_call.expressions):
        # Find nested REGEXP_SPLIT_TO_TABLE
        nested_call = None
        for node in arg.walk():
            if isinstance(node, exp.Anonymous) and node.name.upper() == 'REGEXP_SPLIT_TO_TABLE':
                # Found a nested call
                nested_call = node
                break

        if nested_call:
            # Recursively extract from this nested call first
            nested_call = _extract_nested_regexp_split(nested_call, select)

            # Generate alias for this nested call
            nested_alias = _generate_explode_alias()
            col_alias = f"_nested_col_{i}"

            # Extract arguments
            if len(nested_call.expressions) >= 2:
                nested_str_expr = nested_call.expressions[0]
                nested_pattern = nested_call.expressions[1]

                # Create SPLIT_BY_REGEXP -> EXPLODE -> LATERAL VIEW
                split_func = exp.Anonymous(
                    this="SPLIT_BY_REGEXP",
                    expressions=[nested_str_expr.copy(), nested_pattern.copy()]
                )
                explode_func = exp.Explode(this=split_func)
                lateral = exp.Lateral(
                    this=explode_func,
                    view=True,
                    alias=exp.TableAlias(
                        this=exp.to_identifier(nested_alias),
                        columns=[exp.to_identifier(col_alias)]
                    )
                )

                # Add to SELECT's laterals
                existing_laterals = select.args.get("laterals") or []
                select.set("laterals", existing_laterals + [lateral])

                # Replace the nested call with column reference
                col_ref = exp.Column(
                    this=exp.to_identifier(col_alias),
                    table=exp.to_identifier(nested_alias)
                )
                new_expressions.append(col_ref)
        else:
            # No nested call, keep original
            new_expressions.append(arg)

    # Update function call arguments
    func_call.set("expressions", new_expressions)
    return func_call


def _contains_regexp_split_to_table(node: exp.Expression) -> bool:
    """
    Check if an expression contains any REGEXP_SPLIT_TO_TABLE calls.

    Args:
        node: The expression node to check

    Returns:
        True if contains REGEXP_SPLIT_TO_TABLE, False otherwise
    """
    for child in node.walk():
        if child is not node and isinstance(child, exp.Anonymous) and child.name.upper() == 'REGEXP_SPLIT_TO_TABLE':
            return True
    return False


def _wrap_select_with_lateral_view(select: exp.Select, call_info: dict) -> None:
    """
    Wrap a SELECT with a LATERAL VIEW for one REGEXP_SPLIT_TO_TABLE call.

    This function modifies the SELECT in-place by:
    1. Removing the REGEXP_SPLIT_TO_TABLE expression
    2. Adding a LATERAL VIEW with EXPLODE(SPLIT_BY_REGEXP(...))
    3. Replacing the original expression with a column reference

    Args:
        select: The SELECT statement to modify
        call_info: Dictionary containing call metadata (index, alias, call, original_expr)
    """
    func_call = call_info['call']
    alias_name = call_info['alias']
    expr_index = call_info['index']

    # Extract arguments: REGEXP_SPLIT_TO_TABLE(string_expr, pattern)
    args = func_call.expressions
    if len(args) < 2:
        return  # Invalid call, skip

    string_expr = args[0]
    pattern = args[1]

    # Check if string_expr contains nested REGEXP_SPLIT_TO_TABLE
    has_nested = False
    for node in string_expr.walk():
        if isinstance(node, exp.Anonymous) and node.name.upper() == 'REGEXP_SPLIT_TO_TABLE':
            has_nested = True
            break

    if has_nested:
        # Recursively process the nested call first
        # Create a temporary select to process the inner expression
        # For now, we'll handle this iteratively by processing all calls
        pass

    # Generate unique alias for LATERAL VIEW table
    table_alias = _generate_explode_alias()

    # Create SPLIT_BY_REGEXP function call for Doris
    split_func = exp.Anonymous(
        this="SPLIT_BY_REGEXP",
        expressions=[string_expr.copy(), pattern.copy()]
    )

    # Wrap in EXPLODE
    explode_func = exp.Explode(this=split_func)

    # Create LATERAL VIEW
    lateral = exp.Lateral(
        this=explode_func,
        view=True,
        alias=exp.TableAlias(
            this=exp.to_identifier(table_alias),
            columns=[exp.to_identifier(alias_name)]
        )
    )

    # Replace the REGEXP_SPLIT_TO_TABLE expression with column reference
    col_ref = exp.Column(
        this=exp.to_identifier(alias_name),
        table=exp.to_identifier(table_alias)
    )

    # Update the SELECT expressions
    new_expressions = list(select.expressions)
    new_expressions[expr_index] = col_ref
    select.set("expressions", new_expressions)

    # Add LATERAL VIEW
    existing_laterals = select.args.get("laterals") or []
    select.set("laterals", existing_laterals + [lateral])


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


def remove_with_data_clause(expression: exp.Expression) -> exp.Expression:
    """
    Remove WITH DATA / WITH NO DATA clause from CREATE TABLE AS SELECT statements.

    PostgreSQL supports:
        CREATE TABLE t AS SELECT * FROM source;              (default with data)
        CREATE TABLE t AS SELECT * FROM source WITH DATA;    (explicit with data)
        CREATE TABLE t AS SELECT * FROM source WITH NO DATA; (structure only)

    Doris only supports:
        CREATE TABLE t AS SELECT * FROM source;              (always with data)

    This function removes the WITH DATA / WITH NO DATA clause to ensure Doris compatibility.

    Args:
        expression: The AST to process

    Returns:
        The processed AST with WITH DATA clauses removed
    """
    # Find all CREATE statements
    for create in expression.find_all(exp.Create):
        # Check if this is CREATE TABLE AS SELECT
        if create.args.get("this") and create.args.get("expression"):
            # Check if there are properties
            properties = create.args.get("properties")
            if properties and hasattr(properties, "expressions"):
                # Filter out WithDataProperty
                new_props = [
                    prop for prop in properties.expressions
                    if not isinstance(prop, exp.WithDataProperty)
                ]

                # Update properties
                if new_props:
                    properties.set("expressions", new_props)
                else:
                    # Remove properties entirely if empty
                    create.set("properties", None)

    return expression


def fix_lateral_view_ambiguity(expression: exp.Expression) -> exp.Expression:
    """
    Fix column name ambiguity in WHERE clauses after LATERAL VIEW conversion.

    When LATERAL VIEW generates a column with the same name as an original table column,
    references to that column in WHERE clauses become ambiguous. This function adds
    table name prefixes to disambiguate such references.

    Example:
        Before fix:
            SELECT ... FROM table
            LATERAL VIEW EXPLODE(...) tmp AS col
            WHERE COALESCE(col, '') ...  ← Ambiguous: table.col or tmp.col?

        After fix:
            SELECT ... FROM table
            LATERAL VIEW EXPLODE(...) tmp AS col
            WHERE COALESCE(table.col, '') ...  ← Clear: refers to original table.col

    Args:
        expression: The AST to process

    Returns:
        The processed AST with ambiguity resolved
    """
    # Find all SELECT statements with LATERAL VIEW
    for select in expression.find_all(exp.Select):
        # Check if this SELECT has LATERAL VIEWs
        laterals = select.args.get("laterals")
        if not laterals:
            continue

        # Collect column names generated by LATERAL VIEWs
        lateral_columns = set()
        for lateral in laterals:
            if isinstance(lateral, exp.Lateral) and lateral.args.get("alias"):
                alias = lateral.args["alias"]
                if isinstance(alias, exp.TableAlias) and alias.args.get("columns"):
                    for col in alias.args["columns"]:
                        if isinstance(col, exp.Identifier):
                            lateral_columns.add(col.this)

        if not lateral_columns:
            continue

        # Find the base table name
        from_expr = select.args.get("from")
        if not from_expr:
            continue

        table_name = None
        if isinstance(from_expr, exp.From):
            table_expr = from_expr.this
            if isinstance(table_expr, exp.Table):
                table_ident = table_expr.args.get("this")
                if isinstance(table_ident, exp.Identifier):
                    table_name = table_ident.this

        if not table_name:
            continue

        # Fix WHERE clause column references
        where = select.args.get("where")
        if where:
            _add_table_prefix_to_columns(where, lateral_columns, table_name)

    return expression


def _add_table_prefix_to_columns(
    node: exp.Expression,
    lateral_columns: set,
    table_name: str
) -> None:
    """
    Recursively add table prefix to column references that may be ambiguous.

    Args:
        node: The AST node to process
        lateral_columns: Set of column names generated by LATERAL VIEW
        table_name: The base table name to use as prefix
    """
    for child in node.walk():
        # Look for Column nodes without table prefix
        if isinstance(child, exp.Column):
            col_name = child.this
            if isinstance(col_name, exp.Identifier) and col_name.this in lateral_columns:
                # Check if this column already has a table prefix
                if not child.args.get("table"):
                    # Add table prefix
                    child.set("table", exp.to_identifier(table_name))


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


def preserve_ascii_function(expression: exp.Expression) -> exp.Expression:
    """
    Preserve ASCII() function instead of converting to ORD(CONVERT(...)).

    Doris natively supports ASCII() function, so we don't need the complex conversion
    that SQLGlot performs when transpiling from PostgreSQL.

    PostgreSQL's ascii(col) is parsed as UNICODE(col) by SQLGlot, and when generating
    to Doris it becomes ORD(CONVERT(col USING utf32)). We convert it back to ASCII(col).

    Converts:
        UNICODE(col) -> ASCII(col)

    Args:
        expression: The expression tree to process

    Returns:
        Modified expression with ASCII preserved

    Example:
        >>> from sqlglot import parse
        >>> from sqlglot.contrib.doris_transpile import preserve_ascii_function
        >>> tree = parse("SELECT ascii(name)", read="postgres")[0]
        >>> result = preserve_ascii_function(tree)
        >>> result.sql(dialect="doris")
        'SELECT ASCII(name)'
    """
    # Find all Unicode nodes (PostgreSQL's ascii() is parsed as UNICODE())
    for node in expression.find_all(exp.Unicode):
        # Replace UNICODE(col) with ASCII(col)
        inner_col = node.this
        ascii_func = exp.Anonymous(this="ASCII", expressions=[inner_col])
        node.replace(ascii_func)

    return expression


def convert_date_format_patterns(expression: exp.Expression) -> exp.Expression:
    """
    Convert Java-style date format patterns to MySQL-style for STR_TO_DATE.

    Doris supports both Java-style (yyyyMMdd) and MySQL-style (%Y%m%d) formats,
    but MySQL-style is more standard and consistent across Doris functions.

    Converts:
        'yyyyMMdd' -> '%Y%m%d'
        'yyyy-MM-dd' -> '%Y-%m-%d'
        'yyyy-MM-dd HH:mm:ss' -> '%Y-%m-%d %H:%i:%s'

    Args:
        expression: The expression tree to process

    Returns:
        Modified expression with converted date formats

    Example:
        >>> from sqlglot import parse
        >>> from sqlglot.contrib.doris_transpile import convert_date_format_patterns
        >>> tree = parse("SELECT str_to_date('20200101', 'yyyyMMdd')", read="postgres")[0]
        >>> result = convert_date_format_patterns(tree)
        >>> result.sql(dialect="doris")
        "SELECT STR_TO_DATE('20200101', '%Y%m%d')"
    """
    # Java to MySQL format mapping
    # Order matters - process longer patterns first to avoid partial replacements
    format_mapping = [
        ('yyyy', '%Y'),  # 4-digit year
        ('yy', '%y'),    # 2-digit year
        ('MM', '%m'),    # Month (01-12)
        ('dd', '%d'),    # Day (01-31)
        ('HH', '%H'),    # Hour (00-23)
        ('hh', '%h'),    # Hour (01-12)
        ('mm', '%i'),    # Minutes (00-59) - MySQL uses %i for minutes
        ('ss', '%s'),    # Seconds (00-59)
        ('SSS', '%f'),   # Milliseconds
        ('a', '%p'),     # AM/PM
    ]

    # Find all StrToDate and StrToTime nodes
    for node in list(expression.find_all(exp.StrToDate)) + list(expression.find_all(exp.StrToTime)):
        format_arg = node.args.get("format")
        if format_arg and isinstance(format_arg, exp.Literal):
            original_format = format_arg.this
            if original_format and isinstance(original_format, str):
                # Check if it's Java-style format (contains 'yyyy', 'MM', 'dd', etc.)
                if any(java_pattern in original_format for java_pattern, _ in format_mapping):
                    # Smart handling for lowercase 'mm':
                    # If format contains time separators (: or space before/after time patterns),
                    # treat 'mm' as minutes. Otherwise, treat as month (common mistake).
                    has_time_separator = ':' in original_format or (
                        ('HH' in original_format or 'hh' in original_format) and
                        (' ' in original_format or 'T' in original_format)
                    )

                    # Convert Java format to MySQL format
                    new_format = original_format
                    for java_pattern, mysql_pattern in format_mapping:
                        # Special handling for lowercase 'mm'
                        if java_pattern == 'mm' and not has_time_separator:
                            # In pure date format (no time separator), treat 'mm' as month
                            new_format = new_format.replace('mm', '%m')
                        else:
                            new_format = new_format.replace(
                                java_pattern, mysql_pattern)

                    # Update the format literal
                    format_arg.set("this", new_format)

    return expression


def add_where_to_delete(expression: exp.Expression) -> exp.Expression:
    """
    Add WHERE 1=1 to DELETE statements that have no WHERE clause.

    Doris requires DELETE statements to have a WHERE clause. Without it,
    the DELETE statement will fail with a syntax error.

    Before (PostgreSQL):
        DELETE FROM schema1.table1;

    After (Doris):
        DELETE FROM schema1.table1 WHERE 1 = 1;

    Args:
        expression: The AST to process

    Returns:
        The processed AST with WHERE 1=1 added to DELETE statements
    """
    for delete in expression.find_all(exp.Delete):
        if delete.args.get("where") is None:
            # Add WHERE 1 = 1
            where_clause = exp.Where(
                this=exp.EQ(
                    this=exp.Literal.number(1),
                    expression=exp.Literal.number(1),
                )
            )
            delete.set("where", where_clause)

    return expression


def _is_nextval_call(node: exp.Expression) -> bool:
    """Check if a node is a NEXTVAL(...) function call."""
    if isinstance(node, exp.Anonymous):
        try:
            return node.name.upper() == "NEXTVAL"
        except Exception:
            pass
    return False


def _make_null_literal() -> exp.Expression:
    """Build an AST node that renders as the bare SQL keyword ``NULL``.

    Doris' AUTO_INCREMENT column will fill in a generated value whenever the
    source row's value is ``NULL``, so we emit a plain SQL NULL for every
    ``NEXTVAL(...)`` call we rewrite. ``NULL`` is accepted by Doris both in
    SELECT lists and in VALUES tuples, unlike the ``DEFAULT`` keyword which
    is only valid inside VALUES/SET.
    """
    return exp.Null()


def replace_nextval_with_default(expression: exp.Expression) -> exp.Expression:
    """
    Replace ``NEXTVAL('seq')`` with a plain ``NULL`` inside INSERT statements
    so Doris' AUTO_INCREMENT column generates the value.

    The function is named ``replace_nextval_with_default`` for historical
    reasons (the target used to be the ``DEFAULT`` keyword). We ultimately
    settled on ``NULL`` because Doris' parser only accepts the ``DEFAULT``
    keyword inside VALUES/SET contexts — any ``SELECT ... DEFAULT ...``
    clause is rejected with ``mismatched input ... expecting '('``, so a
    single uniform rewrite target is preferable for both shapes.

    Supported shapes:

    * ``INSERT INTO t (...) SELECT ..., NEXTVAL('seq') [AS id], ... FROM ...``
      Each ``NEXTVAL`` in a top-level SELECT expression is replaced with
      ``NULL``; an existing ``AS <alias>`` is preserved
      (``NEXTVAL('s') AS id`` → ``NULL AS id``). The INSERT column list is
      left untouched.

    * ``INSERT INTO t (...) VALUES (..., NEXTVAL('seq'), ...)``
      Each ``NEXTVAL`` at the top level of a VALUES tuple is replaced with
      ``NULL``.

    * ``GROUP BY NEXTVAL('seq')`` inside an ``INSERT ... SELECT`` —
      because the value is rewritten to a constant ``NULL`` (which is not
      a meaningful ``GROUP BY`` expression), any matching top-level
      ``NEXTVAL`` in the GROUP BY is dropped. If the GROUP BY becomes
      empty as a result, it is removed entirely.

    Deliberately NOT handled:

    * ``UPDATE t SET col = NEXTVAL('s')`` — left as-is, will surface as a
      Doris error so the author can migrate explicitly.
    * Bare ``SELECT NEXTVAL('s')`` outside an INSERT — same rationale.
    * ``NEXTVAL`` inside ``WHERE`` or nested expressions such as
      ``COALESCE(id, NEXTVAL('s'))`` — only top-level replacements are
      safe; the rest is left for Doris to reject.
    * Other PostgreSQL sequence functions (``CURRVAL`` / ``SETVAL`` /
      ``LASTVAL``) — their semantics do not map to AUTO_INCREMENT.

    Before (PostgreSQL)::

        INSERT INTO t (id, data_dt, node_no)
        SELECT NEXTVAL('seq') AS id, data_dt, txn_node_no FROM tmp

    After (Doris)::

        INSERT INTO t (id, data_dt, node_no)
        SELECT NULL AS id, data_dt, txn_node_no FROM tmp

    Args:
        expression: The AST to process (modified in place and returned).

    Returns:
        The processed AST.
    """
    if not isinstance(expression, exp.Insert):
        return expression

    body = expression.args.get("expression")
    if body is None:
        return expression

    if isinstance(body, exp.Select):
        for i, sel_expr in enumerate(list(body.expressions)):
            if isinstance(sel_expr, exp.Alias):
                if _is_nextval_call(sel_expr.this):
                    sel_expr.set("this", _make_null_literal())
            elif _is_nextval_call(sel_expr):
                body.expressions[i] = _make_null_literal()

        # Strip top-level NEXTVAL(...) entries from GROUP BY. A constant
        # ``NULL`` is not a useful GROUP BY expression, and the original
        # NEXTVAL was usually only there to satisfy PostgreSQL's "every
        # SELECT expression must appear in GROUP BY" rule.
        group = body.args.get("group")
        if group is not None and hasattr(group, "expressions"):
            new_group_exprs = [
                g for g in group.expressions if not _is_nextval_call(g)
            ]
            if len(new_group_exprs) != len(group.expressions):
                if new_group_exprs:
                    group.set("expressions", new_group_exprs)
                else:
                    body.set("group", None)

    elif isinstance(body, exp.Values):
        for tup in body.expressions:
            if not isinstance(tup, exp.Tuple):
                continue
            for i, item in enumerate(list(tup.expressions)):
                if _is_nextval_call(item):
                    tup.expressions[i] = _make_null_literal()

    return expression


# --- Backward-compatible aliases (kept so external callers keep working) ---
# Previous name dropped the whole column; the new behaviour keeps the column
# and only rewrites the value, but we preserve the old symbol so imports like
# ``from sqlglot.contrib.doris_transpile import drop_sequence_columns`` still
# resolve.
drop_sequence_columns = replace_nextval_with_default


# --------------------------------------------------------------------------- #
# PostgreSQL AGE() rewrite                                                    #
# --------------------------------------------------------------------------- #

# Map ``EXTRACT(<unit> FROM AGE(end, start))`` to the equivalent Doris
# expression. PG's AGE returns a structured interval (years/months/days/...),
# so EXTRACT pulls a *component* of that interval, NOT the total span between
# the two timestamps. The translations below honour PG's modular semantics:
#
#   AGE('2024-03-15', '2020-01-10') = 4 years 2 mons 5 days
#     EXTRACT(YEAR  FROM ...) = 4
#     EXTRACT(MONTH FROM ...) = 2
#     EXTRACT(DAY   FROM ...) = 5
#
# DAY is borrow-aware: PG's day component can be larger than ``end-start`` of
# the day-of-month numbers when the end day-of-month is smaller (e.g.
# AGE('2024-03-01', '2024-02-28') -> 0 mons 2 days, even though
# DAY(end) - DAY(start) = -27). The fix is to add the previous month's length
# (i.e. the day count of the month *before* ``end``) when the naive
# subtraction underflows. This matches PG byte-for-byte across the
# tested workloads.
#
# HOUR/MINUTE/SECOND emit the simple component diff and log a warning. A
# rigorous borrow-aware rewrite would require nested CASE/IF and is deferred
# until a real workload needs it; in practice these EXTRACT variants of AGE
# are extremely rare.
_AGE_TIME_COMPONENT_FUNCS: t.Dict[str, str] = {
    "HOUR": "HOUR",
    "MINUTE": "MINUTE",
    "SECOND": "SECOND",
}


def _is_age_call(node: exp.Expression) -> bool:
    """True if ``node`` is a ``AGE(end, start)`` Anonymous call."""
    return (
        isinstance(node, exp.Anonymous)
        and node.name
        and node.name.upper() == "AGE"
        and len(node.expressions) == 2
    )


def _build_age_extract_replacement(
    unit: str, end: exp.Expression, start: exp.Expression
) -> t.Optional[exp.Expression]:
    """
    Build the Doris-equivalent expression for ``EXTRACT(<unit> FROM AGE(end, start))``.

    Returns ``None`` if the unit is not supported (caller should leave the
    original EXTRACT untouched and log a warning).
    """
    unit_upper = unit.upper()

    if unit_upper == "YEAR":
        # EXTRACT(YEAR FROM AGE(end, start)) -> TIMESTAMPDIFF(YEAR, start, end)
        return exp.TimestampDiff(
            this=end.copy(),
            expression=start.copy(),
            unit=exp.Var(this="YEAR"),
        )

    if unit_upper == "MONTH":
        # EXTRACT(MONTH FROM AGE(end, start)) ->
        #   TIMESTAMPDIFF(MONTH, start, end) % 12
        # PG's MONTH component is the residual (0..11), not the total.
        diff = exp.TimestampDiff(
            this=end.copy(),
            expression=start.copy(),
            unit=exp.Var(this="MONTH"),
        )
        return exp.Mod(this=diff, expression=exp.Literal.number(12))

    if unit_upper == "QUARTER":
        # EXTRACT(QUARTER FROM AGE(end, start)) ->
        #   FLOOR((TIMESTAMPDIFF(MONTH, start, end) % 12) / 3) + 1
        # PG returns quarters 1..4 (not 0..3): month residual 0..2 -> Q1,
        # 3..5 -> Q2, 6..8 -> Q3, 9..11 -> Q4. Verified against live GP 7:
        #   AGE('2024-03-15','2020-01-10') = 4y 2m 5d -> residual month 2 -> Q1,
        #   AGE('2025-01-15','2020-05-20') = 4y 7m 26d -> residual month 7 -> Q3.
        diff = exp.TimestampDiff(
            this=end.copy(),
            expression=start.copy(),
            unit=exp.Var(this="MONTH"),
        )
        residual = exp.Mod(this=diff, expression=exp.Literal.number(12))
        floored = exp.Floor(
            this=exp.Div(this=residual, expression=exp.Literal.number(3))
        )
        return exp.Add(this=floored, expression=exp.Literal.number(1))

    if unit_upper == "DAY":
        # EXTRACT(DAY FROM AGE(end, start)) is borrow-aware in PG:
        #   if DAY(end) >= DAY(start): DAY(end) - DAY(start)
        #   else: DAY(end) - DAY(start) + DAY(LAST_DAY(end - INTERVAL 1 MONTH))
        # The else branch borrows a full month's worth of days from the month
        # before ``end`` (which is the month being "completed" when going from
        # start -> end), matching PG byte-for-byte on the tested workloads.
        day_end = exp.Anonymous(this="DAY", expressions=[end.copy()])
        day_start = exp.Anonymous(this="DAY", expressions=[start.copy()])
        naive_diff = exp.Sub(this=day_end.copy(), expression=day_start.copy())

        # Borrow term: DAY(LAST_DAY(end - INTERVAL 1 MONTH))
        end_minus_one_month = exp.Anonymous(
            this="DATE_SUB",
            expressions=[
                end.copy(),
                exp.Interval(this=exp.Literal.number(1), unit=exp.Var(this="MONTH")),
            ],
        )
        borrow_term = exp.Anonymous(
            this="DAY",
            expressions=[exp.Anonymous(this="LAST_DAY", expressions=[end_minus_one_month])],
        )
        with_borrow = exp.Add(this=naive_diff.copy(), expression=borrow_term)

        case = exp.Case(
            ifs=[
                exp.If(
                    this=exp.GTE(this=day_end, expression=day_start),
                    true=naive_diff,
                )
            ],
            default=with_borrow,
        )
        # Wrap in Paren so adjacent ``*`` / ``+`` / etc. don't sneak inside.
        return exp.Paren(this=case)

    if unit_upper in _AGE_TIME_COMPONENT_FUNCS:
        func_name = _AGE_TIME_COMPONENT_FUNCS[unit_upper]
        # Build ``FUNC(end) - FUNC(start)`` as a Paren so adjacent operators
        # (e.g. multiplication) bind correctly. This is the *naive* component
        # diff and may differ from PG when the lower components borrow across
        # this unit; logged at warning level by the caller.
        return exp.Paren(
            this=exp.Sub(
                this=exp.Anonymous(this=func_name, expressions=[end.copy()]),
                expression=exp.Anonymous(this=func_name, expressions=[start.copy()]),
            )
        )

    return None


def convert_age_in_extract(expression: exp.Expression) -> exp.Expression:
    """
    Rewrite ``EXTRACT(<unit> FROM AGE(end, start))`` into Doris-compatible SQL.

    Doris supports neither PostgreSQL's ``AGE()`` (which returns an interval)
    nor ``EXTRACT(<unit> FROM <interval>)``. The combined ``EXTRACT(... FROM
    AGE(...))`` pattern is, however, by far the most common real-world usage
    (typically composed as ``12 * EXTRACT(YEAR ...) + EXTRACT(MONTH ...)`` to
    compute the months between two dates). This transform handles that
    pattern losslessly.

    Bare ``AGE(a, b)`` calls (not wrapped in EXTRACT) are intentionally left
    untouched so Doris reports them as unsupported, which is preferable to
    silently producing a string with different semantics. The user can decide
    case-by-case how to rewrite them.

    Before (PostgreSQL):
        SELECT 12 * EXTRACT(YEAR  FROM AGE(t2.term_dt, t2.start_dt))
             +      EXTRACT(MONTH FROM AGE(t2.term_dt, t2.start_dt))
        FROM t2

    After (Doris):
        SELECT 12 * TIMESTAMPDIFF(YEAR,  t2.start_dt, t2.term_dt)
             +      TIMESTAMPDIFF(MONTH, t2.start_dt, t2.term_dt) % 12
        FROM t2

    Args:
        expression: The AST to process.

    Returns:
        The processed AST (modified in place; same object returned for
        convenience so this composes with the other transforms).
    """
    for extract in list(expression.find_all(exp.Extract)):
        unit_node = extract.this
        age_node = extract.expression

        if not _is_age_call(age_node):
            continue

        # ``EXTRACT(<unit> FROM ...)`` parses unit as a Var (e.g. Var('YEAR'))
        # in both PG and Doris dialects, but be defensive about Identifier and
        # Literal forms too just in case.
        if isinstance(unit_node, exp.Var):
            unit_text = unit_node.name
        elif isinstance(unit_node, exp.Identifier):
            unit_text = unit_node.this
        elif isinstance(unit_node, exp.Literal) and unit_node.is_string:
            unit_text = unit_node.this
        else:
            unit_text = unit_node.sql() if unit_node else ""

        end = age_node.expressions[0]
        start = age_node.expressions[1]

        replacement = _build_age_extract_replacement(unit_text, end, start)
        if replacement is None:
            logger.warning(
                "convert_age_in_extract: unsupported EXTRACT unit %r in %s; "
                "Doris will reject this statement, please rewrite manually.",
                unit_text,
                _short_sql(extract),
            )
            continue

        extract.replace(replacement)

    return expression


def convert_date_arithmetic(expression: exp.Expression) -> exp.Expression:
    """
    Convert date arithmetic (date +/- integer) to DATE_ADD/DATE_SUB with INTERVAL.

    PostgreSQL supports `date - 1` to subtract 1 day from a date, but Doris
    does not support arithmetic with plain integers on dates. Doris requires
    DATE_ADD/DATE_SUB with INTERVAL syntax.

    Before (PostgreSQL):
        date(date_trunc('month', DATE '2026-02-10')) - 1
        current_date - 7
        DATE '2026-01-01' + 30

    After (Doris):
        DATE_ADD(DATE_TRUNC(CAST('2026-02-10' AS DATE), 'MONTH'), INTERVAL -1 DAY)
        DATE_ADD(CURRENT_DATE, INTERVAL -7 DAY)
        DATE_ADD(CAST('2026-01-01' AS DATE), INTERVAL 30 DAY)

    Args:
        expression: The AST to process

    Returns:
        The processed AST with date arithmetic converted
    """
    for node in list(expression.find_all(exp.Sub, exp.Add)):
        left = node.this
        right = node.expression

        # Pattern: date_expr +/- integer_literal
        if isinstance(right, exp.Literal) and not right.is_string and _is_date_expression(left):
            n = int(right.this)
            if isinstance(node, exp.Sub):
                n = -n

            # Unwrap redundant DATE() wrapper
            # DATE(DATE_TRUNC(...)) → DATE_TRUNC(...)
            date_expr = left.this if isinstance(left, exp.Date) else left

            # Build DATE_ADD(date_expr, INTERVAL n DAY)
            date_add = exp.DateAdd(
                this=date_expr.copy(),
                expression=exp.Literal.number(n),
                unit=exp.Var(this="DAY"),
            )
            node.replace(date_add)

    return expression


# --------------------------------------------------------------------------- #
# Compound INTERVAL expansion (PostgreSQL → Doris)
# --------------------------------------------------------------------------- #

# Matches one ``<sign?><digits><spaces><word>`` segment inside a PostgreSQL
# compound interval literal such as ``'1 month -1 day'`` or ``'1 year 2 months'``.
_COMPOUND_INTERVAL_RE = re.compile(r"([+-]?\d+)\s*([A-Za-z]+)")


def _parse_compound_interval(s: str) -> t.List[t.Tuple[int, str]]:
    """
    Parse a PostgreSQL compound interval literal into ``(value, UNIT)`` pairs.

    Plural unit names (``DAYS``, ``MONTHS`` ...) are normalized to their
    singular form because Doris/MySQL only accept the singular keyword.

    Examples:
        '1 month -1 day'        -> [(1, 'MONTH'), (-1, 'DAY')]
        '1 year 2 months'       -> [(1, 'YEAR'), (2, 'MONTH')]
        '5 days'                -> [(5, 'DAY')]
        ''                      -> []
    """
    out: t.List[t.Tuple[int, str]] = []
    for n_str, unit in _COMPOUND_INTERVAL_RE.findall(s):
        u = unit.upper()
        # MONTHS -> MONTH, DAYS -> DAY, HOURS -> HOUR, etc.
        # Keep MS/US-style abbreviations (no trailing S to strip) unchanged.
        if len(u) > 1 and u.endswith("S"):
            u = u[:-1]
        out.append((int(n_str), u))
    return out


def expand_compound_interval(expression: exp.Expression) -> exp.Expression:
    """
    Expand PostgreSQL compound INTERVAL literals into single-unit INTERVALs.

    PostgreSQL supports compound interval literals like ``INTERVAL '1 month -1 day'``,
    but Doris/MySQL only accept the single-unit form ``INTERVAL n UNIT``. This
    transform rewrites the surrounding ``+/-`` chain so that each unit becomes
    its own ``INTERVAL`` operand.

    Rules:
        - Single-unit literal (compound parse yields exactly one segment):
          rewrite in place. ``INTERVAL '5 days'`` becomes ``INTERVAL 5 DAY``.
        - Multi-unit literal: the parent expression must be ``Add`` or ``Sub``.
          The outer sign is multiplied into every segment (PostgreSQL semantics
          for ``X - INTERVAL '1 month -1 day'`` is ``X - 1 month + 1 day``).
          The chain is then rebuilt as ``base ± INTERVAL n1 U1 ± INTERVAL n2 U2 ...``.
        - Multi-unit literal whose parent is not ``Add``/``Sub`` (rare; e.g.
          ``SELECT INTERVAL '1 day -1 hour'``): left unchanged. Doris will
          report an error at execution time, which is preferable to silently
          producing an arithmetically different expression.

    Examples:
        SELECT t + INTERVAL '1 month -1 day'
            -> SELECT t + INTERVAL 1 MONTH - INTERVAL 1 DAY

        SELECT t - INTERVAL '1 month -1 day'
            -> SELECT t - INTERVAL 1 MONTH + INTERVAL 1 DAY

        SELECT t + INTERVAL '5 days'
            -> SELECT t + INTERVAL 5 DAY

    Args:
        expression: The AST to process.

    Returns:
        The same AST, mutated in place.
    """
    # ---- Pass 1: numeric-string -> bare-number normalization for single-unit ----
    # Done before compound expansion so that any node duplicated via
    # ``base.copy()`` in Pass 2 already carries the normalized literal.
    #
    # ``interval.unit`` may be one of:
    #   - a real ``exp.Var`` node  -> single-unit interval, structurally fine
    #   - ``None``                  -> no unit attached
    #   - ``False``                 -> sqlglot uses the literal ``False`` in
    #                                  some code paths (observed when an
    #                                  ORDER BY appears in the same SELECT)
    # Both ``None`` and ``False`` are non-truthy and mean "needs expansion",
    # so a truthy check is safer than ``is not None`` here.
    for interval in expression.find_all(exp.Interval):
        if not interval.unit:
            continue
        lit = interval.this
        if (
            isinstance(lit, exp.Literal)
            and lit.is_string
            and lit.this.lstrip("+-").isdigit()
        ):
            interval.set("this", exp.Literal.number(int(lit.this)))

    # ---- Pass 2: expand compound INTERVAL literals ----
    for interval in list(expression.find_all(exp.Interval)):
        if interval.unit:
            continue  # already handled by Pass 1

        lit = interval.this
        if not isinstance(lit, exp.Literal) or not lit.is_string:
            continue

        parts = _parse_compound_interval(lit.this)
        if not parts:
            continue

        # Single segment: rewrite in place. Covers '5 days' -> 5 DAY.
        if len(parts) == 1:
            n, u = parts[0]
            interval.set("this", exp.Literal.number(n))
            interval.set("unit", exp.Var(this=u))
            continue

        # Multiple segments: need a host Add/Sub to attach the chain.
        parent = interval.parent
        if not isinstance(parent, (exp.Add, exp.Sub)):
            # Isolated compound interval (e.g. SELECT INTERVAL '1 day -1 hour').
            # No safe rewrite possible; leave it for Doris to error on.
            continue

        # The INTERVAL must be the RIGHT operand of an Add/Sub (i.e.
        # ``<date_expr> +/- INTERVAL '...'``). If it sits on the LEFT
        # (``INTERVAL '...' +/- <date_expr>``), we can't safely rewrite:
        #   * ``INTERVAL ... - X`` is meaningless in PG (interval minus
        #     timestamp), so we don't touch it.
        #   * ``INTERVAL ... + X`` is technically commutative for date+interval
        #     but the rewrite shape we use (``X + INTERVAL ... - INTERVAL ...``)
        #     would silently change the operand order, which we choose not to
        #     do without an explicit need from a real-world query.
        # Leave such cases unchanged so Doris reports the syntax error
        # explicitly rather than running a semantically different statement.
        if interval is not parent.expression:
            continue

        outer_sign = 1 if isinstance(parent, exp.Add) else -1
        base = parent.this  # the date/time expression on the LHS

        def _make_iv(n: int, u: str) -> exp.Interval:
            return exp.Interval(
                this=exp.Literal.number(abs(n)),
                unit=exp.Var(this=u),
            )

        n0, u0 = parts[0]
        n0 *= outer_sign
        chain: exp.Expression = (exp.Add if n0 >= 0 else exp.Sub)(
            this=base.copy(), expression=_make_iv(n0, u0)
        )
        for n, u in parts[1:]:
            n *= outer_sign
            cls = exp.Add if n >= 0 else exp.Sub
            chain = cls(this=chain, expression=_make_iv(n, u))

        parent.replace(chain)

    return expression


# --------------------------------------------------------------------------- #
# DATE_TRUNC unit normalization (PG plural -> Doris singular)
# --------------------------------------------------------------------------- #

# Doris officially supports only these singular unit names for DATE_TRUNC
# (https://doris.apache.org/docs/dev/sql-manual/sql-functions/scalar-functions/
#  date-time-functions/date-trunc). PG additionally accepts the plural forms
# ('months', 'days', ...). We only strip a trailing ``S`` when the singular
# form lands in this whitelist, so unknown user-supplied units (e.g. an
# unrelated typo) are left alone.
_DATE_TRUNC_UNIT_SINGULARS = {
    "YEAR",
    "QUARTER",
    "MONTH",
    "WEEK",
    "DAY",
    "HOUR",
    "MINUTE",
    "SECOND",
}


def normalize_date_trunc_unit(expression: exp.Expression) -> exp.Expression:
    """
    Normalize DATE_TRUNC unit names to Doris-accepted singular forms.

    PostgreSQL's ``date_trunc`` accepts plural unit names (e.g. ``'months'``,
    ``'days'``). Doris' ``DATE_TRUNC`` documents and enforces a strict
    singular-only whitelist
    (``year|quarter|month|week|day|hour|minute|second``) and raises an error
    on anything else. This transform strips the trailing ``S`` from the
    second-argument unit when (and only when) the resulting singular is in
    that whitelist.

    Examples:
        DATE_TRUNC(x, 'MONTHS')   -> DATE_TRUNC(x, 'MONTH')
        DATE_TRUNC(x, 'DAYS')     -> DATE_TRUNC(x, 'DAY')
        DATE_TRUNC(x, 'QUARTERS') -> DATE_TRUNC(x, 'QUARTER')
        DATE_TRUNC(x, 'MONTH')    -> unchanged
        DATE_TRUNC(x, 'fortnights') -> unchanged (not in whitelist)

    Args:
        expression: The AST to process.

    Returns:
        The same AST, mutated in place.
    """
    for node in expression.find_all(exp.DateTrunc, exp.TimestampTrunc):
        unit = node.args.get("unit")
        if not isinstance(unit, exp.Var):
            continue
        name = unit.name.upper()
        if len(name) > 1 and name.endswith("S") and name[:-1] in _DATE_TRUNC_UNIT_SINGULARS:
            unit.set("this", name[:-1])
    return expression


# --------------------------------------------------------------------------- #
# Multi-column (tuple) IN (subquery) -> EXISTS rewrite
# --------------------------------------------------------------------------- #

# Subquery clauses that make a naive "AND correlation into existing WHERE"
# rewrite semantically unsafe (e.g. GROUP BY happens before WHERE, so pushing
# an outer-correlated predicate into the WHERE would shrink the groups before
# aggregation). When any of these show up we leave the IN alone and warn.
_TUPLE_IN_UNSAFE_CLAUSES = (
    "group",
    "having",
    "distinct",
    "qualify",
    "with",
    "offset",
    "limit",
    "order",
)


def rewrite_tuple_in_subquery(expression: exp.Expression) -> exp.Expression:
    """
    Rewrite multi-column ``(a, b) IN (SELECT x, y FROM t ...)`` to
    ``EXISTS (SELECT 1 FROM t ... WHERE t.x = a AND t.y = b)`` for Doris.

    Doris does not support the SQL-standard row-constructor IN-subquery
    syntax. The equivalent EXISTS form runs in Doris and executes as a
    SEMI JOIN with comparable performance.

    Scope (what IS rewritten):
        - Positive ``(tuple) IN (subquery)`` where the subquery is a plain
          ``SELECT expr_list FROM single_table [WHERE ...]``.
        - Tuple arity must match subquery projection arity.

    Scope (what is NOT rewritten, left as-is with a ``logger.warning`` so
    the user notices during migration):
        - ``NOT IN`` (``In.parent`` is ``exp.Not``). Converting ``NOT IN``
          to ``NOT EXISTS`` silently changes result sets when the subquery
          projection contains NULL rows - see
          ``docs/MULTI_COLUMN_IN_SUBQUERY.md`` section 4.
        - Subquery with GROUP BY / HAVING / DISTINCT / QUALIFY / WITH /
          LIMIT / OFFSET / ORDER or that is a UNION. These need a nested
          wrapping SELECT to preserve semantics; we defer that complexity.
        - Subqueries with joined FROMs (multiple tables). Determining the
          right table prefix for each projection column is non-trivial
          and we do not want to silently misassign.
        - Tuple arity mismatch with subquery arity (malformed SQL).

    Column qualification:
        - The inner projection and the outer tuple must BOTH be qualified,
          otherwise the EXISTS scoping silently collapses to tautology.
          Concretely, inside ``EXISTS (SELECT 1 FROM bl WHERE ... = cust_id)``
          the bare ``cust_id`` would resolve to ``bl.cust_id`` first
          (inner-then-outer scope), yielding ``bl.cust_id = bl.cust_id``
          - always true - which would make UPDATE/DELETE hit every row.
        - Inner bare ``Column`` is prefixed with the subquery's single FROM
          table alias/name.
        - Outer bare ``Column`` is prefixed with the enclosing query's
          primary table alias/name (discovered by walking up the AST to
          the nearest UPDATE/DELETE/SELECT with a single-table FROM).
          If the outer query has a multi-table FROM (JOIN) we cannot
          safely pick a single prefix and therefore SKIP the rewrite with
          a warning - the user should qualify the tuple explicitly.

    Args:
        expression: The AST to process.

    Returns:
        The same AST, mutated in place.
    """
    for in_node in list(expression.find_all(exp.In)):
        tuple_node = in_node.this
        query_node = in_node.args.get("query")

        if not isinstance(tuple_node, exp.Tuple):
            continue
        if not isinstance(query_node, exp.Subquery):
            continue

        # NOT IN: leave untouched. The NULL semantics of NOT IN and
        # NOT EXISTS diverge when the subquery projection can produce NULL,
        # and silently flipping produced SQL is a migration hazard.
        if isinstance(in_node.parent, exp.Not):
            logger.warning(
                "Doris 不支持多列 NOT IN (subquery) 且其 NULL 语义与 NOT EXISTS "
                "不等价，保留原样请手工改写: %s",
                _short_sql(in_node),
            )
            continue

        select = query_node.this
        if not isinstance(select, exp.Select):
            logger.warning(
                "多列 IN 子查询不是普通 SELECT（如 UNION），保留原样: %s",
                _short_sql(in_node),
            )
            continue

        # Any subquery clause that can change the grouping / ordering /
        # cardinality of the inner result set cannot be handled by a naive
        # "AND the correlation into the WHERE" rewrite.
        if any(select.args.get(k) for k in _TUPLE_IN_UNSAFE_CLAUSES):
            logger.warning(
                "多列 IN 子查询含 GROUP BY/HAVING/DISTINCT/LIMIT/ORDER 等子句，"
                "自动转换可能改变语义，保留原样: %s",
                _short_sql(in_node),
            )
            continue

        from_clause = select.args.get("from")
        if from_clause is None:
            # No FROM means nothing sensible to correlate against.
            continue
        if select.args.get("joins"):
            # Multi-table FROM: deciding which side each projection column
            # belongs to requires real scope analysis. Skip conservatively.
            logger.warning(
                "多列 IN 子查询带 JOIN 的 FROM，自动改写列归属不可靠，保留原样: %s",
                _short_sql(in_node),
            )
            continue

        inner_from = from_clause.this
        if not isinstance(inner_from, exp.Table):
            # e.g. FROM (subquery) alias - skip to stay safe.
            continue
        inner_table_ref = inner_from.alias_or_name

        tuple_exprs = tuple_node.expressions
        select_exprs = [e.unalias() for e in select.expressions]

        if len(tuple_exprs) != len(select_exprs):
            logger.warning(
                "多列 IN 元数与子查询列数不等（%d vs %d），保留原样: %s",
                len(tuple_exprs),
                len(select_exprs),
                _short_sql(in_node),
            )
            continue

        # Discover the outer query's primary table so we can qualify bare
        # columns in the tuple. EXISTS scoping resolves bare names to the
        # inner FROM first, so leaving them unqualified here would silently
        # produce ``inner.x = inner.x`` tautologies.
        outer_table_ref = _find_outer_table_ref(in_node)

        # Any bare column in the tuple means we need the outer prefix; if
        # we cannot derive one safely, bail out rather than produce a
        # subtly-wrong rewrite.
        has_bare_outer_col = any(
            isinstance(expr, exp.Column) and not expr.args.get("table")
            for expr in tuple_exprs
        )
        if has_bare_outer_col and outer_table_ref is None:
            logger.warning(
                "多列 IN 外层 tuple 含无前缀列且外层主表不可推断（多表 JOIN 或嵌套子查询）。"
                "自动改写会让 EXISTS 作用域退化为恒真，保留原样请手工加表前缀: %s",
                _short_sql(in_node),
            )
            continue

        # Build the AND-chain of correlation predicates. Each predicate is
        # inner_col = outer_col; both sides get their missing table prefix
        # filled in to avoid the inner-scope tautology trap.
        pairs: t.List[exp.Expression] = []
        for inner_expr, outer_expr in zip(select_exprs, tuple_exprs):
            inner_copy = inner_expr.copy()
            if (
                isinstance(inner_copy, exp.Column)
                and not inner_copy.args.get("table")
                and inner_table_ref
            ):
                inner_copy.set("table", exp.to_identifier(inner_table_ref))

            outer_copy = outer_expr.copy()
            if (
                isinstance(outer_copy, exp.Column)
                and not outer_copy.args.get("table")
                and outer_table_ref
            ):
                outer_copy.set("table", exp.to_identifier(outer_table_ref))

            pairs.append(exp.EQ(this=inner_copy, expression=outer_copy))

        correlation: exp.Expression = pairs[0]
        for pred in pairs[1:]:
            correlation = exp.And(this=correlation, expression=pred)

        # Clone the original SELECT so we retain FROM / WHERE / etc. intact,
        # then swap projection to ``SELECT 1`` and AND the correlation into
        # whatever WHERE already existed.
        new_select = select.copy()
        new_select.set("expressions", [exp.Literal.number(1)])

        existing_where = new_select.args.get("where")
        if existing_where is not None:
            merged = exp.And(
                this=existing_where.this.copy(),
                expression=correlation,
            )
        else:
            merged = correlation
        new_select.set("where", exp.Where(this=merged))

        in_node.replace(exp.Exists(this=new_select))

    return expression


# --------------------------------------------------------------------------- #
# LIKE/ILIKE ANY/ALL (ARRAY[...]) -> OR/AND chain expansion
# --------------------------------------------------------------------------- #


def _any_array_patterns(node: exp.Expression) -> t.Optional[t.List[exp.Expression]]:
    """
    If ``node`` is ``Any(Paren(Array(literals...)))`` or ``Any(Array(...))``,
    return the list of array element expressions. Else return None.

    We deliberately only match direct ``Array`` nodes - subqueries, function
    calls returning arrays, or bare columns typed as array are NOT matched
    (their values are unknown at compile time so we cannot enumerate them).
    """
    if not isinstance(node, exp.Any):
        return None
    inner = node.this
    if isinstance(inner, exp.Paren):
        inner = inner.this
    if isinstance(inner, exp.Array):
        return list(inner.expressions)
    return None


def _all_array_patterns(node: exp.Expression) -> t.Optional[t.List[exp.Expression]]:
    """
    Detect the ``LIKE ALL (ARRAY[...])`` shape. sqlglot represents ``ALL``
    as ``Anonymous(name='ALL', expressions=[Array(...)])`` rather than a
    dedicated node, which is why it's easy to miss.
    """
    if not isinstance(node, exp.Anonymous):
        return None
    if (node.name or "").upper() != "ALL":
        return None
    args = node.expressions
    if len(args) == 1 and isinstance(args[0], exp.Array):
        return list(args[0].expressions)
    return None


def expand_like_any_all_array(expression: exp.Expression) -> exp.Expression:
    """
    Rewrite Doris-incompatible ``col LIKE ANY (ARRAY[...])`` / ``LIKE ALL``
    predicates - including ``NOT LIKE`` and ``ILIKE`` variants - into
    explicit OR/AND chains.

    Background:
        Doris does not support ``LIKE ANY/ALL (array)`` predicates at all.
        When faced with the unsupported form it reports an obscure error
        like "ARRAY<TEXT> cannot be cast to VARCHAR" because its type
        checker sees an ARRAY right operand being fed to LIKE, which
        expects VARCHAR. The fix is to expand the array into explicit
        OR/AND chains so the predicate stops touching ARRAY types at all.

    Rewrite matrix (given ``ps = [p1, p2, ...]``)::

        col LIKE ANY (ARRAY ps)      ->  (col LIKE p1 OR  col LIKE p2  ...)
        col LIKE ALL (ARRAY ps)      ->  (col LIKE p1 AND col LIKE p2  ...)
        col NOT LIKE ANY (ARRAY ps)  ->  (col NOT LIKE p1 AND col NOT LIKE p2 ...)   (De Morgan)
        col NOT LIKE ALL (ARRAY ps)  ->  (col NOT LIKE p1 OR  col NOT LIKE p2 ...)   (De Morgan)

        ILIKE variants are analogous - an ``ILike`` node per pattern is
        generated; a later pass (or the Doris dialect itself) turns each
        ILike into ``LOWER(col) LIKE LOWER(pattern)``.

    Precedence (critical):
        sqlglot does NOT auto-emit precedence parens. ``AND(x, Or(a,b))``
        serializes as ``x AND a OR b`` which is misparsed. We therefore
        ALWAYS wrap the expanded chain in ``exp.Paren`` before replacing.

    Scope:
        - Only ``ARRAY[...]`` / ``ARRAY(...)`` LITERALS are expanded.
        - ``LIKE ANY (SELECT ...)`` or any non-literal RHS is left alone
          with a ``logger.warning``. Rewriting those to a safe Doris form
          (typically EXISTS) is outside this transform's scope.

    Returns:
        The same AST, mutated in place.
    """
    rewrites: t.List[
        t.Tuple[
            exp.Expression,            # node to replace (like_node or its NOT wrapper)
            bool,                      # is_not (was there a NOT wrapping)
            bool,                      # is_ilike
            str,                       # "ANY" or "ALL"
            exp.Expression,            # col expression (LHS of LIKE)
            t.List[exp.Expression],    # the pattern list
        ]
    ] = []

    for like_node in expression.find_all(exp.Like, exp.ILike):
        rhs = like_node.expression
        patterns: t.Optional[t.List[exp.Expression]] = None
        mode: t.Optional[str] = None

        if isinstance(rhs, exp.Any):
            patterns = _any_array_patterns(rhs)
            if patterns is None:
                # Any() wrapping a subquery/function/column - we cannot
                # enumerate its elements statically. Leave as-is and
                # warn; the user will see Doris error and can handle
                # it manually (e.g. rewrite as EXISTS).
                logger.warning(
                    "LIKE/ILIKE ANY (非 ARRAY 字面量: 子查询/函数返回/列) "
                    "Doris 不支持，自动改写未覆盖此形态，保留原样: %s",
                    _short_sql(like_node),
                )
                continue
            mode = "ANY"
        elif isinstance(rhs, exp.Anonymous) and (rhs.name or "").upper() == "ALL":
            patterns = _all_array_patterns(rhs)
            if patterns is None:
                logger.warning(
                    "LIKE/ILIKE ALL (非 ARRAY 字面量) Doris 不支持，"
                    "自动改写未覆盖此形态，保留原样: %s",
                    _short_sql(like_node),
                )
                continue
            mode = "ALL"
        else:
            continue

        if not patterns:
            # Empty array: the predicate is vacuously TRUE (ALL) or
            # FALSE (ANY). We leave it alone - such SQL is almost
            # certainly a bug upstream and silently rewriting would
            # hide it.
            logger.warning(
                "LIKE/ILIKE %s (空 ARRAY) 为恒真/恒假的退化情况，保留原样: %s",
                mode,
                _short_sql(like_node),
            )
            continue

        is_ilike = isinstance(like_node, exp.ILike)
        parent = like_node.parent
        is_not = isinstance(parent, exp.Not)
        target = parent if is_not else like_node
        col = like_node.this

        rewrites.append((target, is_not, is_ilike, mode, col, patterns))

    for target, is_not, is_ilike, mode, col, patterns in rewrites:
        cmp_cls: t.Type[exp.Expression] = exp.ILike if is_ilike else exp.Like

        preds: t.List[exp.Expression] = []
        for pat in patterns:
            pred: exp.Expression = cmp_cls(this=col.copy(), expression=pat.copy())
            if is_not:
                pred = exp.Not(this=pred)
            preds.append(pred)

        # Choose the combining connective. De Morgan flips it whenever
        # there's an outer NOT: ANY/OR and ALL/AND are natural pairs;
        # applying NOT swaps them.
        #
        # Truth table (mode, is_not) -> use_and:
        #   (ANY, False) -> OR    => use_and = False
        #   (ANY, True)  -> AND   => use_and = True
        #   (ALL, False) -> AND   => use_and = True
        #   (ALL, True)  -> OR    => use_and = False
        # Which is exactly: use_and = (mode == "ALL") XOR is_not
        use_and = (mode == "ALL") != is_not
        join_cls: t.Type[exp.Expression] = exp.And if use_and else exp.Or

        combined: exp.Expression = preds[0]
        for p in preds[1:]:
            combined = join_cls(this=combined, expression=p)

        # Always wrap in parens: sqlglot does not emit precedence parens
        # automatically. Omitting this causes ``WHERE flag AND col LIKE ANY(...)``
        # expansion to bind incorrectly as ``flag AND pred1 OR pred2 ...``.
        target.replace(exp.Paren(this=combined))

    return expression


# --------------------------------------------------------------------------- #
# PG TO_CHAR(numeric, fmt) -> Doris CAST(ROUND(x, N) AS STRING) rewrite
# --------------------------------------------------------------------------- #

# Characters allowed in PG numeric format strings (besides '0' and '9').
# Anything else (letters, %, etc.) makes us fall back to the date-format path.
#
# NOTE: '-' / '/' are deliberately EXCLUDED. Although PG numeric format does
# allow a leading '-' / '+' for sign, in practice format strings like
# '0099-09-09' or '20240101' could be a user typing a date literal where a
# format placeholder belongs. Using a hyphen / slash anywhere immediately
# disqualifies the format from being treated as numeric.
_NUMERIC_FMT_NEUTRAL_CHARS = set(" .,+")
_NUMERIC_FMT_SUSPICIOUS_CHARS = set("-/")

# Date-format tokens that appear in PG / MySQL / Doris date format strings.
# Even one of these in the format string means "this is a date format,
# do NOT rewrite as numeric".
#
# We compare against the format string in UPPERCASE.
_DATE_FMT_TOKENS = (
    # PG-style
    "YYYY", "YY", "MM", "MON", "MONTH",
    "DD", "DAY", "DY", "DDD",
    "HH24", "HH12", "HH",
    "MI", "SS", "MS", "US",
    "AM", "PM", "TZ", "WW", "Q",
    # MySQL/Doris style (always lowercase % prefix)
    "%Y", "%M", "%D", "%H", "%I", "%S", "%P", "%W", "%X",
)


def _looks_like_numeric_format(fmt: str) -> t.Optional[int]:
    """
    Return decimal-place count if ``fmt`` is unambiguously a PG numeric format
    string supported by the simplest rewrite (Section 4.1 of
    docs/TO_CHAR_NUMERIC_DORIS.md). Return None otherwise (caller should
    leave the node alone).

    Supported tokens (case-insensitive):
      0 9 . , + - and 'FM' prefix.

    Anything else (D G L C MI PR S RN V EEEE %X YYYY MM ...) -> not supported,
    return None.

    Examples::

        '0.999'      -> 3
        '999.99'     -> 2
        '999'        -> 0
        'FM0.999'    -> 3
        '9,999.99'   -> 2
        'YYYY-MM-DD' -> None  (date)
        'FM999D99'   -> None  (D = locale decimal point, complex)
        '%Y-%m-%d'   -> None  (mysql date)
        ''           -> None  (empty)
    """
    if not fmt:
        return None

    work = fmt
    # Strip leading FM (case-insensitive). FM in PG suppresses padding;
    # for our simple CAST-ROUND output this is the default behavior anyway.
    if work[:2].upper() == "FM":
        work = work[2:]

    if not work:
        return None

    upper = fmt.upper()
    for tok in _DATE_FMT_TOKENS:
        if tok in upper:
            return None

    # If the format contains '-' or '/' it almost certainly is, or was
    # intended to be, a date-shaped string ('0099-09-09', '20240101' style).
    # Refuse to treat it as numeric. Real PG numeric formats use '.' / ','.
    if any(ch in _NUMERIC_FMT_SUSPICIOUS_CHARS for ch in work):
        return None

    has_digit_token = False
    for ch in work:
        if ch in "09":
            has_digit_token = True
            continue
        if ch in _NUMERIC_FMT_NEUTRAL_CHARS:
            continue
        return None

    if not has_digit_token:
        return None

    if "." in work:
        decimal_part = work.split(".", 1)[1]
        return sum(1 for c in decimal_part if c in "09")
    return 0


def rewrite_to_char_numeric(expression: exp.Expression) -> exp.Expression:
    """
    Rewrite PG ``TO_CHAR(numeric, fmt)`` (parsed as ``TimeToStr``) into Doris
    ``CAST(ROUND(x, N) AS STRING)`` when the format string is unambiguously a
    simple numeric format.

    Background:
        PG's ``TO_CHAR`` is polymorphic - given a number it formats numerically,
        given a date it formats temporally. sqlglot collapses both into a single
        ``TimeToStr`` AST node. The default Doris generator emits ``DATE_FORMAT``
        for ``TimeToStr``, which fails at runtime when the first argument is a
        DECIMAL ("Can not find compatibility function signature
        date_format(DECIMALV3(...), VARCHAR)").

    Rewrite (only for simple formats - see ``_looks_like_numeric_format``)::

        TO_CHAR(BASE_RATE, '0.999')   -> CAST(ROUND(BASE_RATE, 3) AS STRING)
        TO_CHAR(x, '999.99')          -> CAST(ROUND(x, 2) AS STRING)
        TO_CHAR(x, '999')             -> CAST(CAST(x AS BIGINT) AS STRING)
        TO_CHAR(x, 'FM0.999')         -> CAST(ROUND(x, 3) AS STRING)
        TO_CHAR(x, '9,999.99')        -> CAST(ROUND(x, 2) AS STRING)
                                          (NOTE: thousand separator is dropped)

    Skipped (left as TimeToStr -> DATE_FORMAT, possibly with a warning when
    the format string is suspiciously non-date-like):

      - Date format strings (``'YYYY-MM-DD'``, ``'%Y-%m-%d'`` etc.) - leave to
        the existing Doris DATE_FORMAT path; fully correct.
      - Complex numeric tokens (``D G L C MI PR S RN V EEEE`` etc.) - keep the
        original form and emit a logger.warning for manual review.
      - Non-literal format (column / function / parameter) - cannot be
        decided statically; warn.

    Caveat (documented in section 4.2 of docs/TO_CHAR_NUMERIC_DORIS.md):
      The rewrite uses ``CAST(ROUND(x, N) AS STRING)`` which DROPS trailing
      zeros (PG ``'1.500'`` -> Doris ``'1.5'``). Per user decision this is
      acceptable for current migration scope. If trailing-zero preservation
      becomes important, switch to a more elaborate concat-based form.

    Returns:
        The same AST, mutated in place.
    """
    rewrites: t.List[t.Tuple[exp.TimeToStr, exp.Expression, int]] = []
    suspicious: t.List[exp.TimeToStr] = []

    for node in expression.find_all(exp.TimeToStr):
        fmt_node = node.args.get("format")
        if fmt_node is None:
            continue

        if not isinstance(fmt_node, exp.Literal) or not fmt_node.is_string:
            # Format is a column, function, parameter etc. - we cannot
            # statically tell whether it's numeric or date-shaped.
            #
            # We do NOT warn here because a runtime-decided format is rare
            # and noisy warnings would drown out actionable signals; if Doris
            # later barfs the user can manually inspect.
            continue

        fmt = fmt_node.this
        n = _looks_like_numeric_format(fmt)
        if n is not None:
            rewrites.append((node, node.this, n))
            continue

        # Format string is NOT a recognized numeric format. Decide whether
        # to emit a warning:
        #
        #   - If it contains a date token (YYYY/MM/DD/...) -> definitely
        #     intended as date format, leave to DATE_FORMAT (correct).
        #   - If it contains '-' or '/' -> likely a date-shaped string used
        #     as a literal output template; DATE_FORMAT will pass through
        #     literal characters fine, no warning needed.
        #   - If it contains ONLY digit / dot / comma / sign / FM characters
        #     plus letters that LOOK LIKE complex numeric tokens (D/G/MI/PR/
        #     S/L/C/RN/V/EEEE) -> almost certainly intended as numeric
        #     format that we cannot translate; warn so user can manually
        #     rewrite.
        #   - Otherwise (e.g. '20240101' literal pass-through) -> stay quiet.
        upper = fmt.upper()
        if any(tok in upper for tok in _DATE_FMT_TOKENS):
            continue
        if any(ch in _NUMERIC_FMT_SUSPICIOUS_CHARS for ch in fmt):
            continue

        # Look for letters that suggest "this was meant as a numeric format
        # token I don't handle".
        complex_numeric_tokens = ("D", "G", "L", "C", "MI", "PR", "S",
                                  "RN", "V", "EEEE", "PL", "SG", "TH")
        if any(tok in upper for tok in complex_numeric_tokens):
            suspicious.append(node)

    for node, value_expr, n in rewrites:
        if n == 0:
            # Integer-only format like '999' or '9999'.
            # PG TO_CHAR(x, '999') rounds half-away-from-zero, so we need
            # ROUND(x, 0) before CAST; otherwise Doris CAST DECIMAL->BIGINT
            # truncates toward zero and diverges from PG (e.g. 100.5 -> 100
            # instead of 101). Wrap in CAST(... AS BIGINT) to drop the fractional
            # zero from the STRING output.
            rounded_int = exp.func(
                "ROUND",
                value_expr.copy(),
                exp.Literal.number(0),
            )
            inner = exp.Cast(
                this=rounded_int,
                to=exp.DataType.build("BIGINT"),
            )
            new_node: exp.Expression = exp.Cast(
                this=inner,
                to=exp.DataType.build("STRING"),
            )
        else:
            rounded = exp.func(
                "ROUND",
                value_expr.copy(),
                exp.Literal.number(n),
            )
            new_node = exp.Cast(
                this=rounded,
                to=exp.DataType.build("STRING"),
            )
        node.replace(new_node)

    for node in suspicious:
        fmt_str = node.args["format"].this
        try:
            value_sql = node.this.sql(dialect="postgres")
        except Exception:
            value_sql = "?"
        logger.warning(
            "PG TO_CHAR(%s, %r) 含 Doris 不支持的复杂数字格式 token "
            "(D/G/MI/PR/S/L/C/RN/V/EEEE 等), 当前保留为 DATE_FORMAT 调用, "
            "Doris 端会因签名不匹配报错, 请手工改写 "
            "(例如 CAST(x AS STRING) 或 FORMAT(x, N))。",
            value_sql,
            fmt_str,
        )

    return expression


# --------------------------------------------------------------------------- #
# DELETE WHERE scalar-subquery -> USING (derived-table) rewrite
# --------------------------------------------------------------------------- #

# Comparison operators whose direct child Subquery we consider a "scalar
# subquery" for the purpose of USING rewrite. IS / IS NOT technically also
# qualify but are far less common in this migration context and bring extra
# NULL-handling nuance; we skip them for now.
_SCALAR_CMP_TYPES: t.Tuple[type, ...] = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.LT,
    exp.GTE,
    exp.LTE,
)


def rewrite_delete_scalar_subquery(expression: exp.Expression) -> exp.Expression:
    """
    Rewrite scalar subqueries in DELETE's WHERE clause to equivalent
    ``DELETE ... USING (derived) ... WHERE ...`` form for Doris.

    Doris does not allow subqueries directly inside DELETE's WHERE clause.
    Starting from Doris 2.x and especially Doris 3.x the ``DELETE ... USING
    ...`` syntax is stable, which this transform targets.

    Example::

        -- PG (input)
        DELETE FROM cdm.xxx
        WHERE BUSI_DATE = (SELECT MAX(data_dt) FROM xx.xx)
          AND TASK_CD = 'xx'

        -- Doris (output)
        DELETE FROM cdm.xxx
        USING (SELECT MAX(data_dt) AS _sq_val_0 FROM xx.xx) AS _sq_0
        WHERE cdm.xxx.BUSI_DATE = _sq_0._sq_val_0
          AND cdm.xxx.TASK_CD = 'xx'

    Scope (what IS rewritten):
        - Scalar subqueries that are the direct operand of a comparison
          (``=`` / ``<>`` / ``>`` / ``<`` / ``>=`` / ``<=``).
        - Multiple scalar subqueries in one DELETE: each becomes its own
          derived table, the first as ``USING <sub>``, the rest attached
          as cross joins on that node - which is exactly how sqlglot models
          ``USING a, b, c``.

    Scope (what is NOT rewritten, left as-is with a ``logger.warning``):
        - DELETE that already has a USING clause (don't clobber).
        - DELETE whose target is not a single ``exp.Table`` (multi-target
          / DELETE against a derived table / etc.).
        - Inner select with more than one projection (not a true scalar
          subquery; likely malformed or an edge case).
        - ``IN (subquery)`` / ``EXISTS (subquery)`` in the WHERE (different
          AST shape; Doris 3.x generally handles these natively or can be
          addressed by the IN-tuple transform).

    Column qualification:
        - After rewriting, bare columns in the WHERE tree that belong to
          the target table are prefixed with the target's alias-or-name to
          disambiguate from the now-visible USING columns. The comparison
          side that was the subquery is already a fully-qualified
          ``_sq_i._sq_val_i`` reference.

    Args:
        expression: The AST to process.

    Returns:
        The same AST, mutated in place.
    """
    for delete in list(expression.find_all(exp.Delete)):
        where = delete.args.get("where")
        if where is None:
            continue

        if delete.args.get("using") is not None:
            # Don't touch manual USING - the user probably knows what
            # they're doing and we'd risk double-merging derived tables.
            continue

        target_table = delete.args.get("this")
        if not isinstance(target_table, exp.Table):
            continue

        # Collect all scalar subqueries in the WHERE subtree.
        scalar_subs: t.List[exp.Subquery] = []
        for sq in where.find_all(exp.Subquery):
            parent = sq.parent
            if isinstance(parent, _SCALAR_CMP_TYPES):
                # Ensure this is a top-level scalar subquery, not
                # something nested inside another subquery's WHERE
                # (find_all goes deep).
                if _is_within_same_where(sq, where):
                    scalar_subs.append(sq)

        if not scalar_subs:
            continue

        # Build a derived table for each scalar subquery. Each gets a
        # unique table alias (_sq_N) and its projection gets a column
        # alias (_sq_val_N) so the replacement reference is unambiguous.
        derived_infos: t.List[t.Tuple[exp.Subquery, str, str]] = []
        for i, sq in enumerate(scalar_subs):
            inner_select = sq.this
            if not isinstance(inner_select, exp.Select):
                logger.warning(
                    "DELETE WHERE 标量子查询不是 SELECT（可能是 UNION），跳过: %s",
                    _short_sql(sq),
                )
                continue

            select_exprs = inner_select.expressions
            if len(select_exprs) != 1:
                logger.warning(
                    "DELETE WHERE 标量子查询返回 %d 列而非 1 列，跳过: %s",
                    len(select_exprs),
                    _short_sql(sq),
                )
                continue

            inner_expr = select_exprs[0]

            # Normalize the inner projection to an aliased form so the
            # outer reference has a predictable column name.
            col_alias_name = f"_sq_val_{i}"
            if isinstance(inner_expr, exp.Alias):
                # Respect user-provided alias; use it as the column name.
                col_alias_name = inner_expr.alias
                aliased_expr = inner_expr.copy()
            else:
                aliased_expr = exp.alias_(inner_expr.copy(), col_alias_name)

            sub_alias_name = f"_sq_{i}"

            new_inner_select = inner_select.copy()
            new_inner_select.set("expressions", [aliased_expr])
            derived = exp.Subquery(
                this=new_inner_select,
                alias=exp.TableAlias(this=exp.to_identifier(sub_alias_name)),
            )

            # Swap the original subquery node with a column reference into
            # the derived table.
            sq.replace(
                exp.Column(
                    this=exp.to_identifier(col_alias_name),
                    table=exp.to_identifier(sub_alias_name),
                )
            )

            derived_infos.append((derived, sub_alias_name, col_alias_name))

        if not derived_infos:
            continue

        # Attach derived tables as USING. sqlglot models ``USING a, b, c``
        # as ``using=a`` with ``joins=[Join(b), Join(c)]`` hanging off a.
        first_derived = derived_infos[0][0]
        if len(derived_infos) > 1:
            extra_joins = [exp.Join(this=d[0]) for d in derived_infos[1:]]
            existing_joins = first_derived.args.get("joins") or []
            first_derived.set("joins", list(existing_joins) + extra_joins)
        delete.set("using", first_derived)

        # Qualify any remaining bare columns in the WHERE tree with the
        # target table's alias-or-name so they're not ambiguous now that
        # the derived tables are in scope.
        target_ref = target_table.alias_or_name
        if target_ref:
            for col in where.find_all(exp.Column):
                if col.args.get("table"):
                    continue
                col.set("table", exp.to_identifier(target_ref))

    return expression


def _is_within_same_where(node: exp.Expression, where: exp.Where) -> bool:
    """
    Return True iff ``node`` is the directly owning ``where``'s descendant,
    i.e. not nested inside another Select/Subquery that reintroduces its
    own WHERE context. We walk up from ``node`` until we hit ``where`` or
    an intervening Select.
    """
    cur: t.Optional[exp.Expression] = node.parent
    while cur is not None and cur is not where:
        if isinstance(cur, exp.Select):
            return False
        cur = cur.parent
    return cur is where


def _find_outer_table_ref(in_node: exp.Expression) -> t.Optional[str]:
    """
    Walk up from an ``In`` node to the nearest enclosing statement and return
    the single-table FROM/target's alias-or-name, or None if the enclosing
    query has a multi-table FROM / joined FROM / subquery FROM / no FROM.

    Used to qualify bare outer-tuple columns when rewriting
    ``(col) IN (SELECT ...)`` as ``EXISTS (... WHERE inner = outer)``.

    Traversal rules:
        - Stop at the first ``Update``/``Delete``/``Select`` ancestor that
          has an identifiable target/FROM.
        - If that statement's target is a single ``exp.Table`` with no
          JOINs, return its alias-or-name.
        - Otherwise return None (caller is expected to bail out).
    """
    node: t.Optional[exp.Expression] = in_node.parent
    while node is not None:
        if isinstance(node, exp.Update):
            tgt = node.args.get("this")
            if isinstance(tgt, exp.Table):
                return tgt.alias_or_name
            return None
        if isinstance(node, exp.Delete):
            tgt = node.args.get("this")
            if isinstance(tgt, exp.Table):
                return tgt.alias_or_name
            return None
        if isinstance(node, exp.Select):
            from_clause = node.args.get("from")
            if from_clause is None:
                return None
            if node.args.get("joins"):
                return None
            first = from_clause.this
            if isinstance(first, exp.Table):
                return first.alias_or_name
            return None
        node = node.parent
    return None


def _short_sql(node: exp.Expression, limit: int = 160) -> str:
    """Best-effort short SQL rendering for log messages."""
    try:
        text = node.sql()
    except Exception:  # pragma: no cover - log path must never raise
        return repr(node)[:limit]
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _is_date_expression(node: exp.Expression) -> bool:
    """
    Check if an expression is a date-type expression.

    Recognizes:
        - DATE(...) function
        - CAST(... AS DATE)
        - DATE_TRUNC / TIMESTAMP_TRUNC
        - CURRENT_DATE / CURRENT_TIMESTAMP
        - Date literal: DATE '2026-01-01'

    Args:
        node: The expression to check

    Returns:
        True if the expression produces a date-type result
    """
    if isinstance(node, exp.Date):
        return True
    if isinstance(node, exp.Cast):
        to_type = node.args.get("to")
        if isinstance(to_type, exp.DataType) and to_type.this in (
            exp.DataType.Type.DATE,
            exp.DataType.Type.DATETIME,
            exp.DataType.Type.TIMESTAMP,
            exp.DataType.Type.TIMESTAMPTZ,
        ):
            return True
    if isinstance(node, (exp.TimestampTrunc, exp.DateTrunc)):
        return True
    if isinstance(node, exp.CurrentDate):
        return True
    if isinstance(node, exp.CurrentTimestamp):
        return True
    # Anonymous functions that return dates
    if isinstance(node, exp.Anonymous) and node.name.upper() in (
        "DATE_TRUNC", "DATE_ADD", "DATE_SUB", "DATE_FORMAT",
    ):
        return True

    return False


# Alternation tokenizer: each match is one of
#   - a single-quoted SQL string (with '' escapes)
#   - a -- line comment
#   - a /* ... */ block comment
#   - the rewrite-target single-segment INTERVAL literal
# The first three are kept verbatim so that we never touch text inside string
# literals or comments; only the last one is rewritten.
_INTERVAL_PREPROCESS_RE = re.compile(
    r"""
    (                                       # group 1: keep verbatim
        '(?:[^']|'')*'                      #   quoted string, '' escapes allowed
      | --[^\n]*                            #   line comment to end-of-line
      | /\*[\s\S]*?\*/                      #   block comment (non-greedy)
    )
    |                                       # OR
    \b(INTERVAL)\s*                         # group 2: INTERVAL keyword
    '\s*([+-]?\d+)\s*([A-Za-z]+)\s*'        # groups 3,4: <sign?><n>, <unit>
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def preprocess_negative_interval(sql: str) -> str:
    """
    Lift the unit out of single-segment INTERVAL literals so the sign is
    preserved by sqlglot's PostgreSQL parser.

    PostgreSQL's parser in sqlglot has a quirk: when the unit is embedded
    inside the literal, the sign is silently dropped. For example:

        INTERVAL '-5 days'   ->  Interval(this='5',  unit=DAYS)   # sign LOST
        INTERVAL '-5' DAY    ->  Interval(this='-5', unit=DAY)    # sign kept

    This preprocessor converts the former into the latter (only for
    *single-segment* literals; compound literals like '1 month -1 day' do not
    match and continue to flow through ``expand_compound_interval``).

    String literals and SQL comments are skipped, so text like
    ``'INTERVAL ''5 days'''`` or ``-- INTERVAL '5 days' is bad`` is left alone.

    Examples:
        INTERVAL '-5 days'  -> INTERVAL '-5' DAY
        INTERVAL '+1 month' -> INTERVAL '+1' MONTH
        INTERVAL '5 day'    -> INTERVAL '5' DAY    (idempotent in semantics)

    Args:
        sql: The raw SQL string to preprocess.

    Returns:
        The preprocessed SQL string.
    """
    def _sub(m: "re.Match[str]") -> str:
        # Group 1 = string literal or comment: keep as-is.
        if m.group(1) is not None:
            return m.group(0)
        # Group 2..4 = INTERVAL '<sign?>n unit': rewrite.
        keyword, n, unit = m.group(2), m.group(3), m.group(4).upper()
        if len(unit) > 1 and unit.endswith("S"):
            unit = unit[:-1]
        return f"{keyword} '{n}' {unit}"

    return _INTERVAL_PREPROCESS_RE.sub(_sub, sql)


def preprocess_date_cast_syntax(sql: str) -> str:
    """
    Preprocess SQL to rewrite DATE'...'::type into CAST(DATE '...' AS type).

    SQLGlot's PostgreSQL parser cannot handle `::` cast directly after a DATE literal.
    This function rewrites the pattern into standard CAST syntax before parsing.

    Patterns handled:
        DATE'20260201'::varchar      → CAST(DATE '20260201' AS varchar)
        DATE '2026-02-01'::text      → CAST(DATE '2026-02-01' AS text)
        date '2026-02-01'::varchar   → CAST(DATE '2026-02-01' AS varchar)

    After SQLGlot parsing and Doris generation:
        → CAST(CAST('2026-02-01' AS DATE) AS VARCHAR)

    Args:
        sql: The raw SQL string to preprocess

    Returns:
        The preprocessed SQL string
    """
    # Match: DATE followed by optional space, then a quoted string, then ::type
    pattern = r"\bDATE\s*'([^']*)'\s*::\s*(\w+)"
    replacement = r"CAST(DATE '\1' AS \2)"
    return re.sub(pattern, replacement, sql, flags=re.IGNORECASE)


class PostgresDoris(Postgres):
    """
    Extended PostgreSQL dialect with Doris-targeted SQL preprocessing.

    Inherits all PostgreSQL parsing behavior, and additionally preprocesses
    SQL to handle patterns that the standard PostgreSQL parser cannot parse
    (e.g., DATE'...'::type).

    This dialect is auto-registered under two keys when this module is imported:
      - 'postgresdoris': explicit name for this enhanced dialect
      - 'postgres': overrides the standard Postgres dialect so that existing code
        using read='postgres' gets the preprocessing automatically

    Usage:
        import sqlglot
        from sqlglot.contrib.doris_transpile import pg_to_doris  # triggers registration

        # Both work identically — 'postgres' is now the enhanced version:
        tree = sqlglot.parse_one("SELECT date'20260201'::varchar", read='postgres')
        tree = sqlglot.parse_one("SELECT date'20260201'::varchar", read='postgresdoris')
    """

    def parse(self, sql: str, **opts) -> t.List[t.Optional[exp.Expression]]:
        sql = preprocess_date_cast_syntax(sql)
        sql = preprocess_negative_interval(sql)
        return super().parse(sql, **opts)

    def parse_into(
        self, expression_type: exp.IntoType, sql: str, **opts
    ) -> t.List[t.Optional[exp.Expression]]:
        sql = preprocess_date_cast_syntax(sql)
        sql = preprocess_negative_interval(sql)
        return super().parse_into(expression_type, sql, **opts)


# Register PostgresDoris as the 'postgres' dialect so that read='postgres'
# transparently includes SQL preprocessing. This uses sqlglot's dialect registry
# (not monkey patching) — only the registry entry is overridden.
from sqlglot.dialects.dialect import _Dialect  # noqa: E402
_Dialect._classes["postgres"] = PostgresDoris


def transpile_to_doris(
    sql: str,
    read: DialectType = None,
    write: DialectType = "doris",
    identity: bool = True,
    error_level: t.Optional[ErrorLevel] = None,
    normalize_mode: str = IdentifierNormalizeMode.TABLE_FULL,
    auto_alias_cast: bool = True,
    explode_to_lateral: bool = True,
    regexp_split_to_lateral: bool = True,
    preserve_ascii: bool = True,
    convert_date_formats: bool = True,
    auto_add_delete_where: bool = True,
    convert_date_arith: bool = True,
    convert_compound_interval: bool = True,
    normalize_date_trunc: bool = True,
    convert_tuple_in_subquery: bool = True,
    convert_delete_scalar_subquery: bool = True,
    expand_like_any_all: bool = True,
    convert_to_char_numeric: bool = True,
    convert_age: bool = True,
    preserve_pg_null_order: bool = False,
    nextval_to_default: bool = True,
    drop_sequences: t.Optional[bool] = None,
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
        regexp_split_to_lateral: Whether to convert REGEXP_SPLIT_TO_TABLE to LATERAL VIEW (default True)
            - SELECT REGEXP_SPLIT_TO_TABLE(col, ',') AS x -> SELECT tmp.x FROM ... LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(col, ',')) tmp AS x
            - PostgreSQL's REGEXP_SPLIT_TO_TABLE is not supported in Doris
        preserve_ascii: Whether to preserve ASCII() function instead of converting to ORD(CONVERT(...)) (default True)
            - Doris natively supports ASCII(), no need for complex conversion
            - ORD(CONVERT(col USING utf32)) -> ASCII(col)
        convert_date_formats: Whether to convert Java-style date formats to MySQL-style (default True)
            - 'yyyyMMdd' -> '%Y%m%d' for STR_TO_DATE
            - Ensures consistency with Doris date format functions
        auto_add_delete_where: Whether to auto-add WHERE 1=1 to DELETE without WHERE (default True)
            - Doris requires DELETE statements to have a WHERE clause
            - DELETE FROM t -> DELETE FROM t WHERE 1 = 1
        convert_date_arith: Whether to convert date +/- integer to DATE_ADD with INTERVAL (default True)
            - Doris doesn't support date - 1 syntax
            - date - 1 -> DATE_ADD(date, INTERVAL -1 DAY)
        convert_compound_interval: Whether to expand PostgreSQL compound INTERVAL literals
            into single-unit INTERVALs joined by +/- (default True)
            - Doris/MySQL only accept INTERVAL n UNIT (single unit)
            - X + INTERVAL '1 month -1 day' -> X + INTERVAL 1 MONTH - INTERVAL 1 DAY
            - X - INTERVAL '1 month -1 day' -> X - INTERVAL 1 MONTH + INTERVAL 1 DAY
              (outer sign is multiplied into every segment, matching PG semantics)
            - Also normalizes single-unit plural forms: INTERVAL '5 days' -> INTERVAL 5 DAY
        normalize_date_trunc: Whether to normalize DATE_TRUNC unit names to Doris-accepted
            singular forms (default True)
            - Doris only accepts year|quarter|month|week|day|hour|minute|second
            - PG accepts plurals: DATE_TRUNC('months', x) -> DATE_TRUNC(x, 'MONTH')
        convert_tuple_in_subquery: Whether to rewrite multi-column ``(tuple) IN (subquery)``
            to an equivalent ``EXISTS (correlated subquery)`` (default True)
            - Doris does not support the SQL-standard row-constructor IN-subquery
            - Only the positive form is rewritten; NOT IN is left as-is with a
              logged warning because NOT IN / NOT EXISTS NULL semantics differ
            - Only simple subqueries are rewritten (single-table FROM, no
              GROUP BY/HAVING/DISTINCT/QUALIFY/WITH/LIMIT/OFFSET/ORDER/UNION)
            - See docs/MULTI_COLUMN_IN_SUBQUERY.md for the full rationale
        convert_delete_scalar_subquery: Whether to rewrite scalar subqueries in
            DELETE's WHERE clause to ``DELETE ... USING (derived) ...`` form
            (default True; requires Doris 2.0+, verified against Doris 3.x)
            - Doris does not allow subqueries inside DELETE's WHERE clause
            - Only scalar subqueries attached to comparison operators
              (=, <>, >, <, >=, <=) are rewritten; IN/EXISTS are handled by
              other transforms or left as-is
            - DELETE with a manually-written USING is left untouched
            - See docs/DELETE_SUBQUERY_DORIS.md for the full rationale
        expand_like_any_all: Whether to rewrite ``col LIKE/ILIKE ANY/ALL (ARRAY[...])``
            predicates into explicit OR/AND chains (default True)
            - Doris does not support ``LIKE ANY/ALL (array)`` and the error
              surfaces as "ARRAY<TEXT> cannot be cast to VARCHAR"
            - Covers 6 variants: {LIKE, NOT LIKE, ILIKE} x {ANY, ALL}
            - De Morgan applied automatically for NOT cases
            - Only ARRAY literals are expanded; subqueries/functions on
              the RHS are left as-is with a warning
            - See docs/LIKE_ANY_ALL_DORIS.md for details
        convert_to_char_numeric: Whether to rewrite PG ``TO_CHAR(numeric, fmt)``
            calls (which sqlglot collapses into ``TimeToStr`` and would emit
            as Doris ``DATE_FORMAT``) into Doris ``CAST(ROUND(x, N) AS STRING)``
            when the format string is unambiguously a simple numeric format
            (default True)
            - Triggered when Doris reports "Can not find compatibility
              function signature: date_format(DECIMAL..., VARCHAR)"
            - Only simple formats (0/9/./,/FM) are rewritten; complex tokens
              (D/G/MI/PR/S/L/C/RN/V/EEEE) are left alone with a warning
            - Date format strings (YYYY/MM/DD/HH/...) are left to the
              existing DATE_FORMAT path - this is correct
            - Trailing zeros are NOT preserved by the simple form; see
              docs/TO_CHAR_NUMERIC_DORIS.md §4.2 for the trade-off
        convert_age: Whether to rewrite ``EXTRACT(<unit> FROM AGE(end, start))``
            into Doris-compatible expressions (default True). Doris supports
            neither ``AGE()`` (which returns an interval) nor
            ``EXTRACT(unit FROM <interval>)``, but the combined ``EXTRACT(...
            FROM AGE(...))`` pattern (typically used as
            ``12 * EXTRACT(YEAR ...) + EXTRACT(MONTH ...)``) is the dominant
            real-world case and is rewritten losslessly.
            - ``EXTRACT(YEAR  FROM AGE(e,s))`` -> ``TIMESTAMPDIFF(YEAR, s, e)``
            - ``EXTRACT(MONTH FROM AGE(e,s))`` ->
              ``TIMESTAMPDIFF(MONTH, s, e) % 12`` (residual months 0..11,
              matching PG semantics — *not* the total month count)
            - ``EXTRACT(QUARTER FROM AGE(e,s))`` ->
              ``FLOOR((TIMESTAMPDIFF(MONTH, s, e) % 12) / 3) + 1``
              (PG returns 1..4, not 0..3; the ``+1`` offset reproduces that)
            - ``EXTRACT(DAY FROM AGE(e,s))`` -> borrow-aware:
              ``CASE WHEN DAY(e) >= DAY(s) THEN DAY(e) - DAY(s)
                     ELSE DAY(e) - DAY(s) + DAY(LAST_DAY(DATE_SUB(e, INTERVAL 1 MONTH))) END``
              (matches PG semantics including the "borrow a month" case
              such as AGE('2024-03-01','2024-02-28') = 0 mons 2 days)
            - ``EXTRACT(HOUR/MIN/SEC FROM AGE(e,s))`` ->
              naive ``(component(e) - component(s))``; correct when
              composed with the higher units, but may differ from PG for
              standalone time-component extraction (rare in practice)
            - Bare ``AGE(a, b)`` not wrapped in EXTRACT is intentionally
              left untouched so Doris reports it as unsupported, rather than
              silently emitting a string with different semantics
            - Unsupported EXTRACT units inside AGE() are left untouched and
              logged at warning level
        preserve_pg_null_order: Whether to faithfully preserve PostgreSQL's default
            NULL ordering when transpiling ORDER BY clauses (default False).
            PG defaults to ASC NULLS LAST / DESC NULLS FIRST, while Doris does
            the opposite (ASC NULLS FIRST / DESC NULLS LAST).
            - False (default): emit Doris' native ordering; ``ORDER BY x`` stays
              ``ORDER BY x``. Faster and shorter SQL, but result row order may
              differ from PG when NULLs are present in the sort column. Use
              this when downstream consumers do not depend on NULL position.
            - True: emit a synthetic ``CASE WHEN x IS NULL THEN 1 ELSE 0 END``
              extra sort key so that the row order matches PG byte-for-byte.
              Use this when migration correctness for NULL-bearing columns is
              required.
        nextval_to_default: Whether to replace ``NEXTVAL('seq')`` with ``NULL``
            inside INSERT statements so Doris' AUTO_INCREMENT column fills the
            value (default True). Kept under this name for historical reasons;
            the actual rewrite target is ``NULL`` because Doris rejects the
            ``DEFAULT`` keyword inside a SELECT list.
            - ``INSERT ... SELECT NEXTVAL('s') AS id, ...``
              → ``INSERT ... SELECT NULL AS id, ...``
            - ``INSERT ... VALUES (NEXTVAL('s'), ...)``
              → ``INSERT ... VALUES (NULL, ...)``
            - Top-level ``NEXTVAL`` in an ``INSERT ... SELECT``'s GROUP BY
              is dropped (NULL is not a meaningful grouping key)
            - The INSERT column list is preserved (nothing is dropped)
            - Assumes Doris target table has AUTO_INCREMENT on the corresponding column
            - Only top-level NEXTVAL calls are replaced; NEXTVAL in UPDATE SET,
              WHERE, or nested expressions is left untouched so Doris can reject
              it explicitly
        drop_sequences: Deprecated alias for ``nextval_to_default``. Kept for
            backward compatibility; if provided, it overrides
            ``nextval_to_default``. The old behaviour that dropped the entire
            column has been superseded by this safer rewrite.
        **opts: Other Generator options (e.g., pretty=True)

    Note:
        PostgreSQL E-strings (E'...') are automatically handled by DorisTranspileGenerator.
        No additional parameter is needed - E-strings are properly converted with correct escaping.

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
            # Preserve ASCII function (before normalization)
            if preserve_ascii:
                expression = preserve_ascii_function(expression)

            # Convert date format patterns (before normalization)
            if convert_date_formats:
                expression = convert_date_format_patterns(expression)

            # Apply identifier normalization
            normalized = normalize_table_identifiers(
                expression,
                source_dialect=read,
                mode=normalize_mode
            )

            # Auto-add alias for CAST
            if auto_alias_cast:
                normalized = add_alias_to_cast(normalized)

            # Remove WITH DATA / WITH NO DATA clause from CREATE TABLE AS SELECT
            normalized = remove_with_data_clause(normalized)

            # Convert REGEXP_SPLIT_TO_TABLE to LATERAL VIEW (must be done before explode_to_lateral)
            if regexp_split_to_lateral:
                normalized = regexp_split_to_table_to_lateral_view(normalized)

            # Convert EXPLODE/UNNEST in SELECT to LATERAL VIEW
            if explode_to_lateral:
                normalized = explode_to_lateral_view(normalized)

            # Fix column name ambiguity in WHERE clauses after LATERAL VIEW conversion
            normalized = fix_lateral_view_ambiguity(normalized)

            # Add WHERE 1=1 to DELETE statements without WHERE clause
            if auto_add_delete_where:
                normalized = add_where_to_delete(normalized)

            # Convert date arithmetic (date +/- integer) to DATE_ADD with INTERVAL
            if convert_date_arith:
                normalized = convert_date_arithmetic(normalized)

            # Expand PostgreSQL compound INTERVAL literals (e.g. '1 month -1 day')
            # into single-unit INTERVALs joined by +/-. Must run AFTER
            # convert_date_arithmetic, which itself can introduce single-unit
            # INTERVAL nodes that this transform must leave untouched.
            if convert_compound_interval:
                normalized = expand_compound_interval(normalized)

            # Normalize DATE_TRUNC unit names to Doris singular forms
            # (PG accepts plurals like 'months'/'days'; Doris does not).
            if normalize_date_trunc:
                normalized = normalize_date_trunc_unit(normalized)

            # Rewrite multi-column (tuple) IN (subquery) to EXISTS; Doris
            # doesn't support the row-constructor IN form. NOT IN is left
            # alone with a warning (NULL semantics differ).
            if convert_tuple_in_subquery:
                normalized = rewrite_tuple_in_subquery(normalized)

            # Rewrite scalar subqueries in DELETE WHERE to USING (derived)
            # form. Doris 2.x/3.x supports DELETE ... USING ... but not
            # subqueries directly inside WHERE.
            if convert_delete_scalar_subquery:
                normalized = rewrite_delete_scalar_subquery(normalized)

            # Expand LIKE/ILIKE ANY/ALL (ARRAY[...]) into OR/AND chains;
            # Doris's LIKE predicate rejects ARRAY right operands.
            if expand_like_any_all:
                normalized = expand_like_any_all_array(normalized)

            # Rewrite PG TO_CHAR(numeric, fmt) (parsed as TimeToStr) to
            # CAST(ROUND(x, N) AS STRING) so Doris doesn't try to feed a
            # decimal into DATE_FORMAT.
            if convert_to_char_numeric:
                normalized = rewrite_to_char_numeric(normalized)

            # Rewrite EXTRACT(<unit> FROM AGE(end, start)) into Doris-native
            # equivalents (TIMESTAMPDIFF / component diff). Bare AGE() is
            # intentionally left alone so Doris flags it as unsupported.
            if convert_age:
                normalized = convert_age_in_extract(normalized)

            # Replace top-level NEXTVAL(...) inside INSERT statements with the
            # DEFAULT keyword so Doris' AUTO_INCREMENT column fills the value.
            # ``drop_sequences`` is the old parameter name; if the caller sets
            # it explicitly we honour it for backward compatibility.
            _nextval_flag = (
                drop_sequences if drop_sequences is not None else nextval_to_default
            )
            if _nextval_flag:
                normalized = replace_nextval_with_default(normalized)

            # Use custom generator for Doris to handle E-strings correctly
            if write == "doris":
                # Pass Generator options (pretty, indent, etc.) to constructor
                # Must pass dialect so IDENTIFIER_START/END use backticks (`) instead of double quotes (")
                generator = DorisTranspileGenerator(
                    dialect=write_dialect,
                    preserve_pg_null_order=preserve_pg_null_order,
                    **opts,
                )
                results.append(generator.generate(normalized, copy=False))
            else:
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
    regexp_split_to_lateral: bool = True,
    nextval_to_default: bool = True,
    drop_sequences: t.Optional[bool] = None,
    **opts,
) -> t.List[str]:
    """
    Shortcut for PostgreSQL to Doris transpilation.

    Example:
        >>> from sqlglot.contrib.doris_transpile import pg_to_doris
        >>> pg_to_doris("SELECT T.id FROM TEST t")
        ['SELECT t.id FROM test AS t']
        >>>
        >>> # NEXTVAL is replaced with NULL so Doris' AUTO_INCREMENT column
        >>> # generates the value. The INSERT column list is kept intact.
        >>> # (``DEFAULT`` cannot appear inside a Doris SELECT list, so we
        >>> # standardise on NULL in both SELECT and VALUES forms.)
        >>> pg_to_doris("INSERT INTO t(id, name) SELECT NEXTVAL('seq'), n FROM src")
        ['INSERT INTO t (id, `name`) SELECT NULL, n FROM src']
        >>> pg_to_doris("INSERT INTO t(id, name) VALUES (NEXTVAL('seq'), 'a')")
        ["INSERT INTO t (id, `name`) VALUES (NULL, 'a')"]
    """
    return transpile_to_doris(
        sql,
        read="postgres",
        write="doris",
        normalize_mode=normalize_mode,
        auto_alias_cast=auto_alias_cast,
        explode_to_lateral=explode_to_lateral,
        regexp_split_to_lateral=regexp_split_to_lateral,
        nextval_to_default=nextval_to_default,
        drop_sequences=drop_sequences,
        **opts
    )


def spark_to_doris(
    sql: str,
    normalize_mode: str = IdentifierNormalizeMode.TABLE_FULL,
    auto_alias_cast: bool = True,
    explode_to_lateral: bool = True,
    regexp_split_to_lateral: bool = True,
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
        regexp_split_to_lateral=regexp_split_to_lateral,
        **opts
    )


def hive_to_doris(
    sql: str,
    normalize_mode: str = IdentifierNormalizeMode.TABLE_FULL,
    auto_alias_cast: bool = True,
    explode_to_lateral: bool = True,
    regexp_split_to_lateral: bool = True,
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
        regexp_split_to_lateral=regexp_split_to_lateral,
        **opts
    )


# Exports
__all__ = [
    "IdentifierNormalizeMode",
    "DorisTranspileGenerator",
    "PostgresDoris",
    "normalize_table_identifiers",
    "add_alias_to_cast",
    "remove_with_data_clause",
    "fix_lateral_view_ambiguity",
    "explode_to_lateral_view",
    "regexp_split_to_table_to_lateral_view",
    "preserve_ascii_function",
    "convert_date_format_patterns",
    "add_where_to_delete",
    "convert_date_arithmetic",
    "expand_compound_interval",
    "normalize_date_trunc_unit",
    "rewrite_tuple_in_subquery",
    "rewrite_delete_scalar_subquery",
    "expand_like_any_all_array",
    "rewrite_to_char_numeric",
    "convert_age_in_extract",
    "drop_sequence_columns",
    "preprocess_date_cast_syntax",
    "preprocess_negative_interval",
    "transpile_to_doris",
    "pg_to_doris",
    "spark_to_doris",
    "hive_to_doris",
]
