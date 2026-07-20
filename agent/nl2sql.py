import json
import logging
from openai import AsyncOpenAI
from common.config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL

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
- 列名和表名用反引号(`)包裹
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
- alias 重命名时不要使用中文

你可以使用以下工具辅助生成SQL:
- lookup_stock_code: 根据公司名称查询股票代码。当用户问题涉及具体股票或公司时，调用此工具获取准确的股票代码。
  参数: company_name (字符串，公司名称或股票名称，如"平安银行"、"贵州茅台")
  工具返回匹配的股票代码信息（code字段如000001.SZ）。如果返回空结果，你需要根据公司名称自行推断股票代码
  （A股格式：6位数字.SZ或6位数字.SH，深交所.SZ，上交所.SH），并在最终SQL中使用推断的代码。
  如果用户问题不涉及具体股票，无需调用此工具，直接生成SQL即可。

工作流程:
1. 分析用户问题，判断是否需要具体股票/公司的股票代码
2. 如果需要，先调用 lookup_stock_code 工具获取股票代码
3. 根据工具返回的股票代码（或自行推断的代码）生成最终SQL
4. 最终只输出SQL语句，不要包含任何解释文字
"""

LOOKUP_STOCK_CODE_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_stock_code",
        "description": "根据公司名称查询股票代码。当用户问题涉及具体股票/公司时，调用此工具获取准确的股票代码。",
        "parameters": {
            "type": "object",
            "properties": {
                "company_name": {
                    "type": "string",
                    "description": "公司名称或股票名称，如\"平安银行\"、\"贵州茅台\"",
                },
            },
            "required": ["company_name"],
        },
    },
}


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


def _execute_stock_code_lookup(company_name: str, executor, schema_name: str, db: str) -> str:
    if not executor:
        return json.dumps(
            {"found": False, "message": "无法查询数据库，请根据公司名称自行推断股票代码（A股格式：6位数字.SZ或6位数字.SH）"},
            ensure_ascii=False,
        )

    safe_name = company_name.replace("'", "''")
    sql = (
        f"SELECT code, name, company_name, short_name "
        f"FROM meta_data.stock_code "
        f"WHERE name LIKE '%{safe_name}%' OR company_name LIKE '%{safe_name}%' OR short_name LIKE '%{safe_name}%'"
    )
    logger.info("Stock code lookup SQL: %s", sql)
    try:
        result = executor.execute_sql(sql, schema_name=schema_name, db=db)
        rows = result.get("rows", [])
        columns = result.get("columns", [])
        if rows:
            return json.dumps(
                {"found": True, "rows": [dict(zip(columns, r)) for r in rows]},
                ensure_ascii=False,
            )
        return json.dumps(
            {"found": False, "message": f"未找到公司'{company_name}'的股票代码，请根据公司名称自行推断股票代码（A股格式：6位数字.SZ或6位数字.SH）"},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.warning("Stock code lookup failed: %s", e)
        return json.dumps(
            {"found": False, "error": str(e), "message": "查询失败，请根据公司名称自行推断股票代码（A股格式：6位数字.SZ或6位数字.SH）"},
            ensure_ascii=False,
        )


async def generate_sql(question: str, schemas: list[dict], executor=None, schema_name: str = "", db: str = "") -> str:
    schema_text = _format_schemas_for_prompt(schemas)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"问题: {question}\n\n候选表结构:\n{schema_text}",
        },
    ]

    client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

    max_tool_rounds = 3
    msg = None
    for _ in range(max_tool_rounds):
        resp = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.1,
            max_tokens=2048,
            tools=[LOOKUP_STOCK_CODE_TOOL],
            tool_choice="auto",
        )

        msg = resp.choices[0].message

        if not msg.tool_calls:
            raw = msg.content or ""
            sql = _extract_sql_from_response(raw)
            logger.info("Generated SQL: %s", sql)
            return sql

        messages.append(msg)

        for tool_call in msg.tool_calls:
            if tool_call.function.name == "lookup_stock_code":
                args = json.loads(tool_call.function.arguments)
                company_name = args.get("company_name", "")
                logger.info("Looking up stock code for: %s", company_name)
                tool_result = _execute_stock_code_lookup(company_name, executor, schema_name, db)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_result,
                })

    raw = (msg.content if msg else "") or ""
    sql = _extract_sql_from_response(raw)
    logger.info("Generated SQL (max rounds reached): %s", sql)
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
