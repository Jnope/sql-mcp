import logging
from openai import AsyncOpenAI
from .config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL

logger = logging.getLogger(__name__)

DIALECT_PROMPT = """
数据库类型: 星环科技 Timelyre 数据库，支持标准SQL、MySQL语句
方言注意事项:
- datetime 格式：2026-07-01 00:00:00
- 建表：
    CREATE TABLE xxx (
        v1 TIMESTAMP,
        v2 STRING,
        v3 CHAR(10),
    )
    STORED AS TIMELYRE
    TBLPROPERTIES (
        "timelyre.tag.cols"="v2",
        "timelyre.timestamp.col"="v1",
        "epoch.engine.enabled"="false"
    );
- ddl的TBLPROPERTIES：
    timelyre.timestamp.col：时间戳列，只允许一列;
    timelyre.tag.cols：允许多列，用逗号分割；
    timelyre.timestamp.col和timelyre.tag.cols组成唯一约束，除以上列还必须至少一个其他列；
    epoch.engine.enabled: 是否开启主键扩展，默认true；
- 股票代码格式: 000001.SZ / 600000.SH
- 时序函数：
    first(<COL>)，返回当前时间序列中时间戳最小的value；
    last(<COL>)，返回当前时间序列中时间戳最大的value；
    inner_first_time(<COL>)，返回当前时间序列中时间戳最小的value对应的时间戳，可以和first搭配使用；
    inner_last_time(<COL>)，返回当前时间序列中时间戳最大的value对应的时间戳
"""

SYSTEM_PROMPT = f"""你是SQL专家，根据输入生成SQL语句。
{DIALECT_PROMPT}
强制遵循以下规则:
- 查询语句默认 LIMIT 100
- 只输出SQL，不要包含任何解释文字，不要用markdown代码块包裹SQL
- 生成SQL中表格式为db_name.table_name
"""

def _format_schemas_for_prompt(schemas: list[dict]) -> str:
    parts = []
    for s in schemas:
        part = f"库:{s['db']}，表:{s['table_name']}，信息:{s['desc']}"
        if s.get("ddl"):
            part += f"\n建表语句:\n{s['ddl']}"
        parts.append(part)
    return "\n\n".join(parts)


def _extract_sql_from_response(raw: str) -> str:
    sql = raw.strip()
    if "```" in sql:
        parts = sql.split("```")
        for part in parts:
            part = part.strip()
            if not part:
                continue
            lines = part.split("\n")
            if len(lines) > 1 or "SELECT" in part.upper() or "WITH" in part.upper() or "CREATE" in part.upper():
                sql = part
                break
        if sql.startswith("sql"):
            sql = sql[3:].strip()
    sql = sql.rstrip(";").strip()
    return sql


async def generate_sql(question: str, schemas: list[dict]) -> str:
    schema_text = _format_schemas_for_prompt(schemas)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"问题: {question}\n\n候选表结构:\n{schema_text}",
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
                "你是数据分析助手。根据用户的查询问题、SQL及执行结果，"
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
