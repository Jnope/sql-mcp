import os
import re
import logging
from openai import AsyncOpenAI

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
5. 表名格式: meta_data.table_name (带库名前缀)
"""

SYSTEM_PROMPT = f"""你是SQL专家，只生成只读SELECT语句。
{DIALECT_PROMPT}
规则:
- 仅允许 SELECT 和 CTE (WITH ... SELECT)
- 必须包含 LIMIT (默认 LIMIT 1000)
- 禁止 DDL/DML/多语句
- 只输出SQL，不要解释，不要用markdown代码块包裹
"""


def _format_schemas_for_prompt(schemas: list[dict]) -> str:
    parts = []
    for s in schemas:
        parts.append(
            f"表: {s['schema_name']}.{s['db']}.{s['table_name']}\n"
            f"信息: {s['doc']}"
        )
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


async def generate_sql(question: str, schemas: list[dict]) -> str:
    schema_text = _format_schemas_for_prompt(schemas)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"表结构:\n{schema_text}\n\n问题: {question}",
        },
    ]

    client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    resp = await client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0.1,
        max_tokens=2048,
    )
    raw = resp.choices[0].message.content or ""
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
