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
from sqlglot.dialects.doris import Doris
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
    Custom Doris Generator that correctly handles PostgreSQL E-strings.

    PostgreSQL E-strings (E'...') are parsed by SQLGlot as ByteString nodes.
    The default Doris generator doesn't handle ByteString properly, losing quotes
    and not handling escape sequences correctly.

    This custom generator overrides bytestring_sql() to:
    1. Convert ByteString to proper quoted string literals
    2. Adjust backslash escaping for regex patterns
    """

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

            # Use custom generator for Doris to handle E-strings correctly
            if write == "doris":
                generator = DorisTranspileGenerator()
                results.append(generator.generate(
                    normalized, copy=False, **opts))
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
        >>> pg_to_doris("SELECT REGEXP_SPLIT_TO_TABLE(col, ',') AS val FROM t")
        ['SELECT _explode_tmp.val FROM t LATERAL VIEW EXPLODE(SPLIT_BY_REGEXP(col, ',')) _explode_tmp AS val']
    """
    return transpile_to_doris(
        sql,
        read="postgres",
        write="doris",
        normalize_mode=normalize_mode,
        auto_alias_cast=auto_alias_cast,
        explode_to_lateral=explode_to_lateral,
        regexp_split_to_lateral=regexp_split_to_lateral,
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
    "normalize_table_identifiers",
    "add_alias_to_cast",
    "remove_with_data_clause",
    "fix_lateral_view_ambiguity",
    "explode_to_lateral_view",
    "regexp_split_to_table_to_lateral_view",
    "preserve_ascii_function",
    "convert_date_format_patterns",
    "transpile_to_doris",
    "pg_to_doris",
    "spark_to_doris",
    "hive_to_doris",
]
