import json
import logging

from fastmcp import FastMCP

from agent.schema_retriever import SchemaRetriever
from agent.nl2sql import generate_sql, explain_result
from agent.sql_validator import validate_readonly, is_select_sql
from agent.executor import Executor
from agent.chart_generator import generate_chart
from agent.config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, RETRIEVE_TOP_N
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
async def search_tables(question: str) -> str:
    """根据自然语言问题，检索相关数据库表结构。

    流程：向量粗筛 top-N → 结合全量表清单经 LLM 推理 → 返回最终相关表结构（含DDL）

    Args:
        question: 用户的自然语言查询问题

    Returns:
        JSON字符串，包含匹配到的表结构信息（表名、字段、类型、DDL、注释）
    """
    coarse = retriever.retrieve(question, top_n=TOP_N_COARSE)
    logger.info("Coarse retrieve top-%d: got %d tables", TOP_N_COARSE, len(coarse))

    all_tables = retriever.list_all_tables_light()

    selected_keys = await _llm_select_tables(question, coarse, all_tables)
    logger.info("LLM selected %d", len(selected_keys))
    logger.debug("LLM selected tables: %s",  selected_keys)

    if not selected_keys:
        return json.dumps(_serialize_schemas(coarse[:5]), ensure_ascii=False)

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

    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def generate_and_execute_sql(question: str) -> str:
    """从自然语言问题生成SQL并执行，返回查询结果。

    完整流程：向量粗筛表结构 → LLM推理选表 → 获取DDL → LLM生成SQL → SELECT则校验执行，非SELECT仅解释

    Args:
        question: 用户的自然语言查询问题

    Returns:
        JSON字符串，包含 sql、result（SELECT时有columns/rows/rowCount，非SELECT时为null）、explanation（AI结论）
    """
    coarse = retriever.retrieve(question, top_n=TOP_N_COARSE)
    logger.info("Coarse retrieve top-%d: got %d tables", TOP_N_COARSE, len(coarse))

    all_tables = retriever.list_all_tables_light()

    selected_keys = await _llm_select_tables(question, coarse, all_tables)
    logger.info("LLM selected %d", len(selected_keys))
    logger.debug("LLM selected tables: %s",  selected_keys)

    if not selected_keys:
        if not coarse:
            return json.dumps(
                {"error": "未检索到相关表结构，请检查向量索引是否已构建"},
                ensure_ascii=False,
            )
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
        explanation = await _safe_explain(question, sql, "该SQL为非查询语句，未执行。")
        return json.dumps(
            {"sql": sql, "result": None, "explanation": explanation},
            ensure_ascii=False,
        )

    authorized = [f"{s['db']}.{s['table_name']}" for s in final_schemas]
    try:
        sql = validate_readonly(sql, authorized_tables=authorized)
    except ValueError as e:
        explanation = await _safe_explain(question, sql, f"SQL校验未通过: {e}")
        return json.dumps(
            {"sql": sql, "result": None, "explanation": explanation},
            ensure_ascii=False,
        )

    schema_name = final_schemas[0]["schema_name"]
    db = final_schemas[0]["db"]
    try:
        result = executor.execute_sql(sql, schema_name=schema_name, db=db)
    except Exception as e:
        explanation = await _safe_explain(question, sql, f"SQL执行失败: {e}")
        return json.dumps(
            {"sql": sql, "result": None, "explanation": explanation},
            ensure_ascii=False,
        )

    row_count = result.get("rowCount", 0)
    col_count = len(result.get("columns", []))
    result_summary = f"返回 {row_count} 行, {col_count} 列"
    if row_count > 0:
        sample = result["rows"][:3]
        result_summary += f"\n前3行: {json.dumps(sample, ensure_ascii=False, default=str)}"

    explanation = await _safe_explain(question, sql, result_summary)

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