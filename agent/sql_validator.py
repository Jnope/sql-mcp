import logging
import re
import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.Merge,
    exp.Command,
    exp.Grant,
    exp.Revoke,
)

DIALECT = "mysql"


def validate_readonly(sql: str, authorized_tables: list[str] | None = None) -> str:
    sql = sql.strip().rstrip(";")

    try:
        statements = sqlglot.parse(sql, read=DIALECT)
    except (sqlglot.errors.ParseError, sqlglot.errors.OptimizeError):
        logger.warning("sqlglot failed to parse SQL, skipping AST validation: %s", sql[:200])
        upper = sql.upper().lstrip()
        if not (upper.startswith("SELECT") or upper.startswith("WITH")):
            raise ValueError("仅允许SELECT语句")
        if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
            sql = f"{sql} LIMIT 100"
            logger.info("Auto-added LIMIT 100 (fallback)")
        return sql

    if not statements or len(statements) > 1:
        raise ValueError("禁止多语句或空语句")

    ast = statements[0]
    if ast is None:
        raise ValueError("无法解析SQL")

    if not isinstance(ast, (exp.Select, exp.With)):
        raise ValueError(f"仅允许SELECT，检测到: {type(ast).__name__}")

    for node in ast.walk():
        node_expr = node[0] if isinstance(node, tuple) else node
        if isinstance(node_expr, FORBIDDEN_NODES):
            raise ValueError(f"禁止的操作: {type(node_expr).__name__}")

    for comment_node in ast.find_all(exp.Comment):
        raise ValueError("禁止SQL注释")

    referenced_tables = []
    for t in ast.find_all(exp.Table):
        full_name = t.name
        if t.db:
            full_name = f"{t.db}.{t.name}"
        referenced_tables.append(full_name)

    if authorized_tables is not None:
        auth_set = set(authorized_tables)
        for ref in referenced_tables:
            if ref not in auth_set and ref.split(".")[-1] not in {
                a.split(".")[-1] for a in auth_set
            }:
                raise ValueError(f"引用了未授权的表: {ref}")

    if not ast.args.get("limit"):
        sql = f"{sql} LIMIT 100"
        logger.info("Auto-added LIMIT 100")

    return sql


def extract_table_names(sql: str) -> list[str]:
    ast = sqlglot.parse_one(sql, read=DIALECT)
    if ast is None:
        return []
    return [t.name for t in ast.find_all(exp.Table)]


def is_select_sql(sql: str) -> bool:
    sql = sql.strip().rstrip(";")
    try:
        statements = sqlglot.parse(sql, read=DIALECT)
    except (sqlglot.errors.ParseError, sqlglot.errors.OptimizeError):
        upper = sql.upper().lstrip()
        return upper.startswith("SELECT") or upper.startswith("WITH")
    if not statements or len(statements) > 1:
        return False
    ast = statements[0]
    if ast is None:
        return False
    return isinstance(ast, (exp.Select, exp.With))


def extract_column_names(sql: str, schema_name: str = "", db: str = "") -> list[str]:
    sql = sql.strip().rstrip(";")
    try:
        statements = sqlglot.parse(sql, read=DIALECT)
    except (sqlglot.errors.ParseError, sqlglot.errors.OptimizeError):
        return []
    if not statements:
        return []
    ast = statements[0]
    if ast is None:
        return []

    select_node = ast
    if isinstance(ast, exp.With):
        select_node = ast.this

    if not isinstance(select_node, exp.Select):
        return []

    columns = []
    has_star = False
    for proj in select_node.expressions:
        if isinstance(proj, exp.Alias):
            columns.append(proj.alias_or_name)
        elif isinstance(proj, exp.Star):
            has_star = True
        else:
            if isinstance(proj, exp.Column):
                columns.append(proj.name)
            else:
                columns.append(proj.alias_or_name or str(proj))

    if has_star and not columns:
        from .executor import Executor
        tables = [t.name for t in select_node.find_all(exp.Table)]
        if tables:
            try:
                ddl = Executor().get_table_ddl(schema_name, db, tables[0])
                if ddl:
                    parsed = sqlglot.parse_one(ddl, read=DIALECT)
                    if isinstance(parsed, exp.Create):
                        schema = parsed.this
                        if isinstance(schema, exp.Schema):
                            columns = [col.name for col in schema.expressions if isinstance(col, exp.ColumnDef)]
            except Exception:
                logger.error(f"failed to get {schema_name}, {db}.{tables[0]} ddl or failed to extract ddl")
                pass

    if has_star and not columns:
        return []

    return columns
