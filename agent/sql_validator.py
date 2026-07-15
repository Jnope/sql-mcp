import logging
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

    statements = sqlglot.parse(sql, read=DIALECT)
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
    statements = sqlglot.parse(sql, read=DIALECT)
    if not statements or len(statements) > 1:
        return False
    ast = statements[0]
    if ast is None:
        return False
    return isinstance(ast, (exp.Select, exp.With))
