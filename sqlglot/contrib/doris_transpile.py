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

import re
import typing as t

from sqlglot import exp, parse
from sqlglot.dialects.dialect import Dialect, NormalizationStrategy
from sqlglot.dialects.doris import Doris
from sqlglot.dialects.postgres import Postgres
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


SEQUENCE_FUNCS = {"NEXTVAL", "CURRVAL", "SETVAL", "LASTVAL", "SEQUENCE"}


def _is_sequence_call(node: exp.Expression) -> bool:
    """Check if a node is a sequence function call (NEXTVAL, CURRVAL, etc.)."""
    if isinstance(node, exp.Anonymous):
        try:
            return node.name.upper() in SEQUENCE_FUNCS
        except Exception:
            pass
    return False


def _expr_contains_sequence(node: exp.Expression) -> bool:
    """Check if an expression or any descendant is a sequence call."""
    if _is_sequence_call(node):
        return True
    for child in node.find_all(exp.Anonymous):
        if _is_sequence_call(child):
            return True
    return False


def drop_sequence_columns(expression: exp.Expression) -> exp.Expression:
    """
    Remove NEXTVAL columns from INSERT INTO ... SELECT statements.

    Assumes Doris target table has AUTO_INCREMENT on the corresponding column,
    so the column and its NEXTVAL expression can simply be removed. Any matching
    NEXTVAL calls in GROUP BY are also removed (they only existed to satisfy
    PostgreSQL's syntax requirement that SELECT columns appear in GROUP BY).

    Only processes INSERT ... SELECT statements with an explicit column list.
    Other statement types are returned unchanged.

    Before (PostgreSQL):
        INSERT INTO t (id, data_dt, node_no)
        SELECT NEXTVAL('seq') AS id, data_dt, txn_node_no
        FROM tmp
        GROUP BY NEXTVAL('seq'), data_dt

    After (Doris):
        INSERT INTO t (data_dt, node_no)
        SELECT data_dt, txn_node_no
        FROM tmp
        GROUP BY data_dt

    Args:
        expression: The AST to process

    Returns:
        The processed AST
    """
    if not isinstance(expression, exp.Insert):
        return expression

    target = expression.this
    select = expression.args.get("expression")

    if target is None or select is None or not isinstance(select, exp.Select):
        return expression

    columns = list(target.expressions) if hasattr(target, "expressions") else []
    if not columns:
        return expression

    # Find which SELECT positions contain sequence calls
    seq_indices: t.Set[int] = set()
    for i, sel_expr in enumerate(select.expressions):
        actual = sel_expr.this if isinstance(sel_expr, exp.Alias) else sel_expr
        if _expr_contains_sequence(actual):
            seq_indices.add(i)

    if not seq_indices:
        return expression

    # Remove sequence columns from INSERT column list
    new_columns = [c for i, c in enumerate(columns) if i not in seq_indices]
    target.set("expressions", new_columns)

    # Remove corresponding SELECT expressions
    new_sel_exprs = [e for i, e in enumerate(select.expressions) if i not in seq_indices]
    select.set("expressions", new_sel_exprs)

    # Remove matching NEXTVAL calls from GROUP BY
    group = select.args.get("group")
    if group:
        group_exprs = group.expressions if hasattr(group, "expressions") else []
        new_group_exprs = [
            g for g in group_exprs if not _expr_contains_sequence(g)
        ]
        if not new_group_exprs:
            select.set("group", None)
        else:
            group.set("expressions", new_group_exprs)

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
    preserve_pg_null_order: bool = False,
    drop_sequences: bool = True,
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
        drop_sequences: Whether to remove NEXTVAL columns from INSERT ... SELECT (default True)
            - Removes the column and NEXTVAL expression from INSERT ... SELECT
            - Also removes matching NEXTVAL from GROUP BY
            - Assumes Doris target table has AUTO_INCREMENT on the corresponding column
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

            # Remove NEXTVAL columns from INSERT ... SELECT
            if drop_sequences:
                normalized = drop_sequence_columns(normalized)

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
    drop_sequences: bool = True,
    **opts,
) -> t.List[str]:
    """
    Shortcut for PostgreSQL to Doris transpilation.

    Example:
        >>> from sqlglot.contrib.doris_transpile import pg_to_doris
        >>> pg_to_doris("SELECT T.id FROM TEST t")
        ['SELECT t.id FROM test AS t']
        >>>
        >>> # NEXTVAL columns are dropped from INSERT ... SELECT
        >>> pg_to_doris("INSERT INTO t(id, name) SELECT NEXTVAL('seq'), n FROM src")
        ["INSERT INTO t (`name`) SELECT n FROM src"]
    """
    return transpile_to_doris(
        sql,
        read="postgres",
        write="doris",
        normalize_mode=normalize_mode,
        auto_alias_cast=auto_alias_cast,
        explode_to_lateral=explode_to_lateral,
        regexp_split_to_lateral=regexp_split_to_lateral,
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
    "drop_sequence_columns",
    "preprocess_date_cast_syntax",
    "preprocess_negative_interval",
    "transpile_to_doris",
    "pg_to_doris",
    "spark_to_doris",
    "hive_to_doris",
]
