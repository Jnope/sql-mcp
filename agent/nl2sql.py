import os
import json
import logging
from openai import AsyncOpenAI
from .executor import Executor

logger = logging.getLogger(__name__)

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "not-needed")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:14b")

DIALECT_PROMPT = """
数据库类型: Apache Doris (TimelyRe)
方言注意事项:
1. 时间函数: date_trunc('day', dt), FROM_UNIXTIME(unix_ts)
2. 股票代码格式: 000001.SZ / 600000.SH
3. 分区查询建议带分区键过滤 (trade_day / datetime)
4. LIMIT 语法: LIMIT 10
5. 表名格式: schema.db.table_name (带库名前缀和 schema)
"""

SYSTEM_PROMPT = f"""你是SQL专家，只生成只读SELECT语句。
{DIALECT_PROMPT}
规则:
- 仅允许 SELECT 和 CTE (WITH ... SELECT)
- 必须包含 LIMIT (默认 LIMIT 1000)
- 禁止 DDL/DML/多语句
- 只输出SQL，不要解释，不要用markdown代码块包裹
"""

TOOL_GET_DDL = {
    "type": "function",
    "function": {
        "name": "get_table_ddl",
        "description": "获取指定表的完整建表语句（DDL），包含列名、类型、分区键等",
        "parameters": {
            "type": "object",
            "properties": {
                "schema_name": {"type": "string", "description": "schema/实例名，如 quark1"},
                "db": {"type": "string", "description": "数据库名，如 meta_data"},
                "table_name": {"type": "string", "description": "表名"},
            },
            "required": ["schema_name", "db", "table_name"],
        },
    },
}

TOOL_DISPATCH = {
    "get_table_ddl": lambda args: _get_executor().get_table_ddl(
        schema_name=args["schema_name"],
        db=args.get("db"),
        table_name=args["table_name"],
    ),
}

_executor: Executor | None = None


def _get_executor() -> Executor:
    global _executor
    if _executor is None:
        _executor = Executor()
    return _executor


def _format_schemas_for_prompt(schemas: list[dict]) -> str:
    parts = []
    for s in schemas:
        part = f"表: {s['schema_name']}.{s['db']}.{s['table_name']}\n信息: {s['doc']}"
        if s.get("ddl"):
            part += f"\n建表语句:\n{s['ddl']}"
        parts.append(part)
    return "\n\n".join(parts)


def _extract_sql_from_response(raw: str) -> str:
    sql = raw.strip()
    if sql.startswith("```"):
        lines = sql.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        sql = "\n".join(lines).strip()
    sql = sql.rstrip(";").strip()
    return sql


def _ensure_ddl_for_schemas(schemas: list[dict]) -> list[dict]:
    fixed = []
    executor = _get_executor()
    for s in schemas:
        s = dict(s)
        if not s.get("ddl"):
            ddl = executor.get_table_ddl(s["schema_name"], s.get("db"), s["table_name"])
            s["ddl"] = ddl
            if ddl:
                logger.info("Fetched DDL for %s.%s.%s from timelyre", s["schema_name"], s["db"], s["table_name"])
        fixed.append(s)
    return fixed


async def generate_sql(question: str, schemas: list[dict]) -> str:
    schemas = _ensure_ddl_for_schemas(schemas)
    schema_text = _format_schemas_for_prompt(schemas)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"候选表结构:\n{schema_text}\n\n问题: {question}\n\n如需获取更多表的详细信息，请使用 get_table_ddl 工具。",
        },
    ]

    client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    resp = await client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0.1,
        max_tokens=2048,
        tools=[TOOL_GET_DDL],
    )

    msg = resp.choices[0].message

    while msg.tool_calls:
        messages.append({"role": "assistant", "content": msg.content, "tool_calls": msg.tool_calls})
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            handler = TOOL_DISPATCH.get(tc.function.name)
            if handler is None:
                result = f"未知工具: {tc.function.name}"
                logger.warning("Unknown tool call: %s", tc.function.name)
            else:
                result = handler(args)
                logger.info("Tool call: %s(%s) -> %d chars", tc.function.name, args.get("table_name", args), len(str(result)))
            if not result:
                result = "未找到该表的建表语句" if tc.function.name == "get_table_ddl" else ""
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(result),
            })

        resp = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.1,
            max_tokens=2048,
            tools=[TOOL_GET_DDL],
        )
        msg = resp.choices[0].message

    raw = msg.content or ""
    sql = _extract_sql_from_response(raw)
    logger.info("Generated SQL: %s", sql)
    return sql


async def explain_result(question: str, sql: str, result_summary: str) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "你是数据分析助手。根据用户的查询问题和SQL执行结果，"
                "用简洁的中文给出数据结论。包括：直接回答、关键数值、异常说明。"
                "不要超过300字。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"问题: {question}\n"
                f"SQL: {sql}\n"
                f"结果摘要: {result_summary}"
            ),
        },
    ]

    client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    resp = await client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0.3,
        max_tokens=1024,
    )
    return resp.choices[0].message.content or ""
