import json
import logging
import os
import sys
import uuid

import sqlglot
from fastmcp import FastMCP
from fastmcp.server.context import Context

from agent.schema_retriever import SchemaRetriever
from agent.nl2sql import generate_sql, explain_result
from agent.sql_validator import validate_readonly, is_select_sql
from agent.executor import Executor
from agent.chart_generator import generate_chart
from agent.config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, RETRIEVE_TOP_N, MAX_RETURN_ROWS, MAX_CHART_ROWS, AVAILABLE_SCHEMAS
from agent.utils.log_util import setup_logging
from openai import AsyncOpenAI

setup_logging()
logger = logging.getLogger("sql-agent")

mcp = FastMCP("sql-agent")

retriever = SchemaRetriever()
executor = Executor()

TOP_N_COARSE = RETRIEVE_TOP_N


def _serialize_schemas(schemas: list[dict]) -> list[dict]:
    result = []
    for s in schemas:
        result.append({
            "schema_name": s["schema_name"],
            "db": s["db"],
            "table_name": s["table_name"],
            "desc": s["desc"],
            "ddl": s.get("ddl", ""),
            "types": s["types"],
            "distance": s.get("_distance", 0),
        })
    return result


def _table_key(s: dict) -> str:
    return f"{s['schema_name']}.{s['db']}.{s['table_name']}"


async def _safe_explain(question: str, sql: str, result_summary: str) -> str:
    try:
        return await explain_result(question, sql, result_summary)
    except Exception as e:
        logger.warning("explain_result failed: %s", e)
        return ""


async def _set_ctx_state(ctx: Context = None, payload: dict = None):
    if ctx is None or payload is None:
        return
    await ctx.set_state("last_sql_result", payload)


def _strip_limit(sql: str) -> str:
    try:
        ast = sqlglot.parse_one(sql, read="mysql")
        if ast.args.get("limit"):
            ast.set("limit", None)
        return ast.sql(dialect="mysql")
    except Exception:
        return sql


def _run_sql(sql: str, schema_name: str = "", db: str = "") -> dict:
    if not is_select_sql(sql):
        return {"error": "仅允许SELECT语句"}
    try:
        sql = validate_readonly(sql)
    except ValueError as e:
        return {"error": f"SQL校验未通过: {e}"}
    sql = _strip_limit(sql)
    sql = f"{sql} LIMIT {MAX_CHART_ROWS}"
    try:
        result = executor.execute_sql(sql, schema_name=schema_name, db=db)
    except Exception as e:
        return {"error": f"SQL执行失败: {e}"}
    return result


async def _generate_and_execute_internal(question: str) -> dict:
    """核心SQL生成+执行流水线，供 generate_and_execute_sql 和 generate_echarts_config 复用。

    Returns:
        成功: {"sql": str, "result": dict, "schema_name": str, "db": str}
        失败: {"error": str, "sql": str (可选)}
    """
    coarse = retriever.retrieve(question, top_n=TOP_N_COARSE)
    logger.info("Coarse retrieve top-%d: got %d tables", TOP_N_COARSE, len(coarse))

    all_tables = retriever.list_all_tables_light()

    selected_keys = await _llm_select_tables(question, coarse, all_tables)
    logger.info("LLM selected %d", len(selected_keys))
    logger.debug("LLM selected tables: %s", selected_keys)

    if not selected_keys:
        if not coarse:
            return {"error": "未检索到相关表结构，请检查向量索引是否已构建"}
        raw = [_table_key(c) for c in coarse[:3]]
        selected_keys = [(k.split(".", 2)) for k in raw]
        selected_keys = [(s, d, t) for s, d, t in selected_keys]

    ddl_map = retriever.get_ddl_by_names(selected_keys)

    final_schemas = []
    for s, d, t in selected_keys:
        entry = {"schema_name": s, "db": d, "table_name": t}
        for hit in coarse:
            if hit["schema_name"] == s and hit.get("db") == d and hit["table_name"] == t:
                entry["desc"] = hit["desc"]
                entry["types"] = hit["types"]
                break
            else:
                entry["desc"] = ""
                entry["types"] = []
        ddl = ddl_map.get((s, d or "", t), "")
        if not ddl:
            try:
                ddl = executor.get_table_ddl(s, d, t)
                logger.info("Fetched DDL from timelyre for %s.%s.%s", s, d, t)
            except Exception as e:
                logger.warning("Failed to fetch DDL for %s.%s.%s: %s", s, d, t, e)
        entry["ddl"] = ddl
        final_schemas.append(entry)

    sql = await generate_sql(question, final_schemas)
    logger.info("Generated sql: %s", sql)

    if not is_select_sql(sql):
        return {"error": "生成的SQL为非查询语句，无法执行", "sql": sql}

    authorized = [f"{s['db']}.{s['table_name']}" for s in final_schemas]
    try:
        sql = validate_readonly(sql, authorized_tables=authorized)
    except ValueError as e:
        return {"error": f"SQL校验未通过: {e}", "sql": sql}

    schema_name = final_schemas[0]["schema_name"]
    db = final_schemas[0]["db"]
    try:
        result = executor.execute_sql(sql, schema_name=schema_name, db=db)
    except Exception as e:
        return {"error": f"SQL执行失败: {e}", "sql": sql, "schema_name": schema_name, "db": db}

    return {"sql": sql, "result": result, "schema_name": schema_name, "db": db}


def _parse_tables_from_llm_output(text: str) -> list[tuple[str, str, str]]:
    tables = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        parts = line.split(".", 2)
        if len(parts) == 3:
            schema, db, table = parts
            tables.append((schema.strip(), db.strip(), table.strip()))
        elif len(parts) == 2:
            tables.append((parts[0].strip(), "", parts[1].strip()))
    return tables


async def _llm_select_tables(
    question: str,
    coarse_hits: list[dict],
    all_tables_light: list[dict],
) -> list[tuple[str, str, str]]:
    coarse_lines = "\n".join(
        f"   表:{_table_key(t)} 描述:{t['desc']} 类型:{','.join(t['types'])}\nddl:{t['ddl']}"
        for t in coarse_hits
    )
    all_lines = "\n".join(
        f"   表:{_table_key(t)} 描述:{t['desc']} 类型:{','.join(t['types'])}"
        for t in all_tables_light
    )
    system_prompt = (
        "你是数据分析专家。根据用户问题，从候选表中选出解决问题所需的表。\n"
        "输出格式：每行一个表，格式为 schema.db.table_name，不要解释不要多余文字。\n"
        "如果粗筛结果不够，可以从全量表清单中选择其他表。\n"
        "只输出需要的表，每行一个，不要序号。"
    )
    user_prompt = (
        f"用户问题：{question}\n\n"
        f"--- 向量粗筛相关表（共{len(coarse_hits)}张）---\n{coarse_lines}\n\n"
        f"--- 全量表清单（共{len(all_tables_light)}张）---\n{all_lines}\n\n"
        "请输出解决问题所需的表（schema.db.table_name），每行一个。"
    )
    client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    resp = await client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
        max_tokens=1024,
    )
    raw = resp.choices[0].message.content or ""
    return _parse_tables_from_llm_output(raw)


@mcp.tool()
async def search_tables(question: str) -> list[dict]:
    """根据自然语言问题，检索相关数据库表结构。

    流程：向量粗筛 top-N → 结合全量表清单经 LLM 推理 → 返回最终相关表结构（含DDL）

    Args:
        question: 用户的自然语言查询问题

    Returns:
        表结构信息列表（表名、字段、类型、DDL、注释）
    """
    coarse = retriever.retrieve(question, top_n=TOP_N_COARSE)
    logger.info("Coarse retrieve top-%d: got %d tables", TOP_N_COARSE, len(coarse))

    all_tables = retriever.list_all_tables_light()

    selected_keys = await _llm_select_tables(question, coarse, all_tables)
    logger.info("LLM selected %d", len(selected_keys))
    logger.debug("LLM selected tables: %s",  selected_keys)

    if not selected_keys:
        logger.warning("failed to select tables by llm")
        return _serialize_schemas(coarse[:5])

    ddl_map = retriever.get_ddl_by_names(selected_keys)

    result = []
    for s, d, t in selected_keys:
        entry: dict = {"schema_name": s, "db": d, "table_name": t}
        for hit in coarse:
            if hit["schema_name"] == s and (hit.get("db") == d or not d) and hit["table_name"] == t:
                entry["desc"] = hit["desc"]
                entry["types"] = hit["types"]
                entry["distance"] = hit.get("_distance", 0)
                break
            else:
                entry["desc"] = ""
                entry["types"] = []
                entry["distance"] = 1.0
        entry["ddl"] = ddl_map.get((s, d or "", t), "")
        result.append(entry)

    return result


@mcp.tool()
async def generate_and_execute_sql(question: str, ctx: Context = None) -> dict:
    """从自然语言问题生成SQL并执行，返回查询结果。

    完整流程：向量粗筛表结构 → LLM推理选表 → 获取DDL → LLM生成SQL → SELECT则校验执行，非SELECT仅解释

    Args:
        question: 用户的自然语言查询问题

    Returns:
        包含 sql、result（SELECT时有columns/rows/rowCount，非SELECT时为null）、explanation（AI结论）
    """
    internal = await _generate_and_execute_internal(question)

    if "error" in internal:
        sql = internal.get("sql", "")
        explanation = await _safe_explain(question, sql, internal["error"])
        payload = {"sql": sql, "result": None, "explanation": explanation,
                   "schema_name": internal.get("schema_name", ""), "db": internal.get("db", "")}
        await _set_ctx_state(ctx, payload)
        return payload

    sql = internal["sql"]
    result = internal["result"]
    schema_name = internal["schema_name"]
    db = internal["db"]

    total_count = result.get("rowCount", 0)
    if total_count > MAX_RETURN_ROWS:
        result["rows"] = result["rows"][:MAX_RETURN_ROWS]
        result["rowCount"] = MAX_RETURN_ROWS
        result["truncated"] = True
        result["totalRowCount"] = total_count
    else:
        result["truncated"] = False
        result["totalRowCount"] = total_count

    row_count = result.get("rowCount", 0)
    col_count = len(result.get("columns", []))
    result_summary = f"返回 {row_count} 行, {col_count} 列"
    if row_count > 0:
        sample = result["rows"][:3]
        result_summary += f"\n前3行: {json.dumps(sample, ensure_ascii=False, default=str)}"

    explanation = await _safe_explain(question, sql, result_summary)
    payload = {"sql": sql, "result": result, "explanation": explanation,
               "schema_name": schema_name, "db": db}
    await _set_ctx_state(ctx, payload)
    return payload


@mcp.tool()
async def generate_echarts_from_last(question: str, chart_type: str = "", title: str = "", ctx: Context = None) -> dict:
    """基于上次SQL的原始结果（去LIMIT重执行获取完整数据）生成ECharts配置，直接返回配置JSON。

    LLM判断数据不适合可视化时返回 {"error": "原因说明"}，不生成图表配置。

    Args:
        question: 用户的图表需求描述（如"展示近三年营收趋势折线图"）
        chart_type: 图表类型（line/bar/pie/scatter/heatmap），为空时LLM自动选择
        title: 图表标题，不传入为空
        ctx: MCP上下文，用于获取历史数据

    Returns:
        成功: ECharts配置字典，可直接用于前端渲染
        失败: {"error": "原因说明"}
    """
    try:
        if ctx is None:
            return {"error": "无MCP上下文"}
        last = await ctx.get_state("last_sql_result")
        if not last or not last.get("sql"):
            return {"error": "无历史SQL可重执行"}
        last_sql = last["sql"]
        schema_name = last.get("schema_name", "")
        db = last.get("db", "")
        parsed = _run_sql(last_sql, schema_name=schema_name, db=db)
        if "error" in parsed:
            logger.error(f"failed to get sql output {str(parsed)}")
            return parsed
        config = await generate_chart(question, parsed, chart_type, title)
        if "error" in config:
            logger.error(f"failed to produce chart config {str(config)}")
            return config
        config["chart_type"] = chart_type or config.get("series", [{}])[0].get("type", "")
        return config
    except Exception as e:
        logger.error(e)
        return {"error": str(e)}


@mcp.tool()
async def generate_echarts_from_sql(question: str, sql: str, schema_name: str = "", db: str = "", chart_type: str = "", title: str = "", ctx: Context = None) -> dict:
    """执行指定SQL并从结果生成ECharts配置，直接返回配置JSON。

    先校验执行SQL获取数据，再根据数据生成图表。schema_name 和 db 可通过 list_schemas / list_databases 获取。

    LLM判断数据不适合可视化时返回 {"error": "原因说明"}，不生成图表配置。

    Args:
        question: 用户的图表需求描述（如"展示近三年营收趋势折线图"）
        sql: SELECT语句
        schema_name: schema名称（必填），可从 list_schemas 获取
        db: 数据库名称（必填），可从 list_databases 获取
        chart_type: 图表类型（line/bar/pie/scatter/heatmap），为空时LLM自动选择
        title: 图表标题，不传入为空
        ctx: MCP上下文，用于获取历史数据

    Returns:
        成功: ECharts配置字典，可直接用于前端渲染
        失败: {"error": "原因说明"}
    """
    try:
        if not schema_name or not db:
            return {"error": "缺少 schema_name 或 db 参数，请先调用 list_schemas 和 list_databases 获取"}
        parsed = _run_sql(sql, schema_name=schema_name, db=db)
        if "error" in parsed:
            logger.error(f"failed to get sql output {str(parsed)}")
            return parsed
        if ctx is not None:
            await _set_ctx_state(ctx, {"sql": sql, "result": parsed, "schema_name": schema_name, "db": db})
        config = await generate_chart(question, parsed, chart_type, title)
        if "error" in config:
            logger.error(f"failed to produce chart config {str(config)}")
            return config
        return config
    except Exception as e:
        logger.error(e)
        return {"error": str(e)}


@mcp.tool()
async def list_schemas() -> dict:
    """获取可用schema列表。来源优先级：AVAILABLE_SCHEMAS 环境变量 > TIMELYRE_PROXY_{NAME} 环境变量模式匹配 > 默认schema。

    Returns:
        成功: {"schemas": [...]}
        失败: {"error": "原因"}
    """
    try:
        if AVAILABLE_SCHEMAS:
            schemas = [s.strip() for s in AVAILABLE_SCHEMAS.split(",") if s.strip()]
            return {"schemas": schemas}

        schemas = set()
        for key, val in os.environ.items():
            if key.startswith("TIMELYRE_PROXY_") and val.strip():
                schemas.add(key[len("TIMELYRE_PROXY_"):].lower())

        if not schemas:
            schemas.add("default")

        return {"schemas": sorted(schemas)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def list_databases(schema_name: str) -> dict:
    """根据schema_name创建连接并执行show databases，返回可用数据库列表。

    Args:
        schema_name: schema名称（通过 list_schemas 获取）

    Returns:
        成功: {"databases": [...]}
        失败: {"error": "原因"}
    """
    try:
        databases = executor.list_databases(schema_name)
        return {"databases": databases}
    except Exception as e:
        return {"error": str(e)}


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
