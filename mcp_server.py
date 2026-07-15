import json
import logging
import os
import sys

from fastmcp import FastMCP

from agent.schema_retriever import SchemaRetriever
from agent.nl2sql import generate_sql, explain_result
from agent.sql_validator import validate_readonly
from agent.executor import Executor
from agent.chart_generator import generate_chart
from agent.utils.log_util import setup_logging

setup_logging()
logger = logging.getLogger("sql-agent")

mcp = FastMCP("sql-agent")

retriever = SchemaRetriever()
executor = Executor()


def _serialize_schemas(schemas: list[dict]) -> list[dict]:
    result = []
    for s in schemas:
        result.append({
            "schema_name": s["schema_name"],
            "db": s["db"],
            "table_name": s["table_name"],
            "doc": s["doc"],
            "ddl": s.get("ddl", ""),
            "types": s["types"],
            "distance": s.get("_distance", 0),
        })
    return result


@mcp.tool()
async def search_tables(question: str) -> str:
    """根据自然语言问题，通过pgvector向量检索相关数据库表结构。

    Args:
        question: 用户的自然语言查询问题

    Returns:
        JSON字符串，包含匹配到的表结构信息（表名、字段、类型、注释、示例数据）
    """
    schemas = retriever.retrieve(question, top_n=5)
    return json.dumps(_serialize_schemas(schemas), ensure_ascii=False)


@mcp.tool()
async def generate_and_execute_sql(question: str) -> str:
    """从自然语言问题生成SQL并执行，返回查询结果。

    完整流程：向量检索表结构 → LLM生成SQL → AST安全校验 → 只读执行

    Args:
        question: 用户的自然语言查询问题

    Returns:
        JSON字符串，包含 sql、result（columns/rows/rowCount）、explanation（AI结论）
    """
    schemas = retriever.retrieve(question, top_n=5)
    if not schemas:
        return json.dumps(
            {"error": "未检索到相关表结构，请检查向量索引是否已构建"},
            ensure_ascii=False,
        )

    sql = await generate_sql(question, _serialize_schemas(schemas))

    authorized = [f"{s['schema_name']}.{s['table_name']}" for s in schemas]
    try:
        sql = validate_readonly(sql, authorized_tables=authorized)
    except ValueError as e:
        return json.dumps({"error": f"SQL校验失败: {e}", "sql": sql}, ensure_ascii=False)

    schema_name = schemas[0]["schema_name"]
    db = schemas[0]["db"]
    try:
        result = executor.execute_sql(sql, schema_name=schema_name, db=db)
    except Exception as e:
        return json.dumps({"error": f"SQL执行失败: {e}", "sql": sql}, ensure_ascii=False)

    row_count = result.get("rowCount", 0)
    col_count = len(result.get("columns", []))
    result_summary = f"返回 {row_count} 行, {col_count} 列"
    if row_count > 0:
        sample = result["rows"][:3]
        result_summary += f"\n前3行: {json.dumps(sample, ensure_ascii=False, default=str)}"

    explanation = ""
    try:
        explanation = await explain_result(question, sql, result_summary)
    except Exception as e:
        logger.warning("explain_result failed: %s", e)

    return json.dumps(
        {"sql": sql, "result": result, "explanation": explanation},
        ensure_ascii=False,
        default=str,
    )


@mcp.tool()
async def execute_python(code: str, data: str = None) -> str:
    """执行Python数据处理代码，可传入SQL查询结果进行加工。

    可用变量: df (传入的DataFrame), pd (pandas), np (numpy), json
    代码需将最终结果赋值给变量 result

    Args:
        code: Python代码字符串
        data: 可选，JSON字符串格式的SQL查询结果（包含columns和rows）

    Returns:
        JSON字符串，包含处理后的数据（columns/rows/rowCount 或 value）
    """
    try:
        result = executor.execute_python(code, data)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def generate_echarts_config(chart_type: str, data: str, title: str = "") -> str:
    """将数据转换为ECharts配置JSON。

    支持的图表类型: line（折线图）, bar（柱状图）, pie（饼图）, scatter（散点图）, heatmap（热力图）

    Args:
        chart_type: 图表类型
        data: JSON字符串，包含columns和rows
        title: 图表标题

    Returns:
        JSON字符串，ECharts配置对象
    """
    try:
        parsed = json.loads(data)
        config = await generate_chart(chart_type, parsed, title)
        return json.dumps(config, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _init():
    try:
        retriever.init_db()
        with retriever.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM table_embeddings")
            count = cur.fetchone()[0]
            logger.info("Index already has %d entries", count)
    except Exception as e:
        logger.error("Failed to initialize vector index: %s", e)
        logger.error("Run with --init flag or ensure pgvector is available")


def main():
    _init()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
