import json
import logging
import pandas as pd
from openai import AsyncOpenAI
from .config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
from .models.chart_model import ChartFields

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是ECharts配置专家。根据用户需求、图表类型和数据列信息，判断数据是否适合可视化。

如果数据适合生成图表，调用 generate_echarts_option 工具生成配置。
如果数据不适合可视化（如单列无关联数据、数据量不足等），调用 reject_chart 工具说明原因。

generate_echarts_option 工具的 fields 参数说明:
- title: 图名称
- chart_type: 图类型 line/bar/pie/scatter/heatmap
- x_axis, y_axis_fields, name_field, value_field 等所有列名字段**必须**使用数据中的实际列名，不能自定义或翻译
- x_axis: 用作 x 轴类目的列名
- y_axis_fields: 用作 y 轴数值的列名列表
- name_field: pie 图中用作名称的列名
- value_field: pie 图中用作数值的列名
- x_value_field: scatter 图中 x 轴数值列名
- y_value_field: scatter 图中 y 轴数值列名
- x_category_field: heatmap 中 x 轴类目列名
- y_category_field: heatmap 中 y 轴类目列名
- value_heatmap_field: heatmap 中数值列名
- series_name: 系列名称
- extra_config: 额外的 ECharts 配置项（如 visualMap, legend 等），会被合并到最终配置中

reject_chart 工具参数:
- reason: 数据不适合可视化的原因

必须调用其中一个工具，不要输出其他文字。
"""

MAX_SAMPLE_ROWS = 5

GENERATE_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_echarts_option",
        "description": "根据样本数据的列名和特征，生成ECharts图配置。包含图表类型选择、数据列映射、系列配置等。所有列名必须使用数据中的实际列名。",
        "parameters": ChartFields.model_json_schema(),
    },
}

REJECT_TOOL = {
    "type": "function",
    "function": {
        "name": "reject_chart",
        "description": "数据不适合可视化时调用，说明原因",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "数据不适合生成图表的原因",
                },
            },
            "required": ["reason"],
        },
    },
}


async def generate_chart(
    question: str,
    data: dict,
    chart_type: str = "",
    title: str = "",
) -> dict:
    columns = data.get("columns", [])
    rows = data.get("rows", [])

    if not columns or not rows:
        raise ValueError("数据为空，无法生成图表")

    sample_rows = rows[:MAX_SAMPLE_ROWS]
    sample_text = json.dumps(sample_rows, ensure_ascii=False, default=str)

    type_hint = f"图表类型: {chart_type}\n" if chart_type else "图表类型: 自动选择\n"

    user_prompt = (
        f"用户需求: {question}\n"
        f"图表标题: {title}\n"
        f"{type_hint}"
        f"数据列: {columns}\n"
        f"前{MAX_SAMPLE_ROWS}行示例:\n{sample_text}\n"
        f"总行数: {len(rows)}\n\n"
        "注意：所有列名字段必须使用上方数据列中的实际列名，不能翻译或自定义。"
    )

    client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    resp = await client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
        max_tokens=1024,
        tools=[GENERATE_TOOL, REJECT_TOOL],
        tool_choice="auto",
    )

    msg = resp.choices[0].message
    if not msg.tool_calls:
        reason = msg.content or "LLM未调用任何工具"
        return {"error": f"数据不适合生成图表: {reason}"}

    tool_call = msg.tool_calls[0]
    tool_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)

    if tool_name == "reject_chart":
        return {"error": f"数据不适合生成图表: {args.get('reason', str(args) or '未知原因')}"}

    if tool_name != "generate_echarts_option":
        return {"error": f"LLM调用了未预期的工具: {tool_name}"}

    fields = ChartFields(**args)

    df = pd.DataFrame(rows, columns=columns)
    option = _build_option(fields, df, title)

    return option


def _build_option(fields: ChartFields, df: pd.DataFrame, title: str) -> dict:
    builder = {
        "line": _build_line_bar,
        "bar": _build_line_bar,
        "pie": _build_pie,
        "scatter": _build_scatter,
        "heatmap": _build_heatmap,
    }.get(fields.chart_type)

    if builder is None:
        builder = _build_line_bar

    option = builder(fields, df, title)
    if "error" in option:
        return option

    option["title"] = {"text": title or fields.title or ""}

    if fields.extra_config:
        option = _deep_merge(option, fields.extra_config)

    return option


def _build_line_bar(fields: ChartFields, df: pd.DataFrame, title: str) -> dict:
    x_col = fields.x_axis or df.columns[0]
    if x_col not in df.columns:
        return {"error": f"列名 '{x_col}' 不在数据列 {list(df.columns)} 中"}

    user_y_cols = fields.y_axis_fields or []
    y_cols = [c for c in user_y_cols if c in df.columns] or [c for c in df.columns if c != x_col]

    x_raw = _safe_list(df[x_col])
    x_is_category = _is_category_type(x_raw)
    x_type = "category" if x_is_category else "time"

    series = []
    for col in y_cols:
        s = {"name": fields.series_name if fields.series_name and len(y_cols) == 1 else col, "type": fields.chart_type}
        if x_is_category:
            s["data"] = _safe_list(df[col])
        else:
            s["data"] = df[[x_col, col]].values.tolist()
        series.append(s)

    result = {
        "title": {"text": title},
        "tooltip": {"trigger": "axis"},
        "legend": {},
        "xAxis": {"type": x_type, "name": x_col},
        "yAxis": {"type": "value"},
        "series": series,
    }
    if x_is_category:
        result["xAxis"]["data"] = x_raw

    return result


def _build_pie(fields: ChartFields, df: pd.DataFrame, title: str) -> dict:
    name_col = fields.name_field or df.columns[0]
    value_col = fields.value_field or df.columns[1]
    if name_col not in df.columns or value_col not in df.columns:
        valid = [c for c in [name_col, value_col] if c in df.columns]
        missing = [c for c in [name_col, value_col] if c not in df.columns]
        return {"error": f"列名 {missing} 不在数据列 {list(df.columns)} 中"}

    data = [
        {"name": _safe_value(row[name_col]), "value": _safe_value(row[value_col])}
        for _, row in df.iterrows()
    ]

    return {
        "title": {"text": title},
        "tooltip": {"trigger": "item"},
        "legend": {"orient": "vertical", "left": "left"},
        "series": [
            {
                "name": fields.series_name or "",
                "type": "pie",
                "radius": "60%",
                "data": data,
            }
        ],
    }


def _build_scatter(fields: ChartFields, df: pd.DataFrame, title: str) -> dict:
    x_col = fields.x_value_field or df.columns[0]
    y_col = fields.y_value_field or df.columns[1]
    if x_col not in df.columns or y_col not in df.columns:
        missing = [c for c in [x_col, y_col] if c not in df.columns]
        return {"error": f"列名 {missing} 不在数据列 {list(df.columns)} 中"}

    data = df[[x_col, y_col]].where(pd.notna(df), None).values.tolist()

    return {
        "title": {"text": title},
        "tooltip": {"trigger": "item"},
        "xAxis": {"type": "value", "name": x_col},
        "yAxis": {"type": "value", "name": y_col},
        "series": [
            {
                "name": fields.series_name or "",
                "type": "scatter",
                "data": data,
            }
        ],
    }


def _build_heatmap(fields: ChartFields, df: pd.DataFrame, title: str) -> dict:
    x_col = fields.x_category_field or df.columns[0]
    y_col = fields.y_category_field or df.columns[1]
    v_col = fields.value_heatmap_field or df.columns[2]
    missing = [c for c in [x_col, y_col, v_col] if c not in df.columns]
    if missing:
        return {"error": f"列名 {missing} 不在数据列 {list(df.columns)} 中"}

    x_cats = df[x_col].unique().tolist()
    y_cats = df[y_col].unique().tolist()

    data = []
    for _, row in df.iterrows():
        x_idx = x_cats.index(row[x_col])
        y_idx = y_cats.index(row[y_col])
        data.append([x_idx, y_idx, _safe_value(row[v_col])])

    return {
        "title": {"text": title},
        "tooltip": {"trigger": "item"},
        "xAxis": {"type": "category", "data": [str(x) for x in x_cats]},
        "yAxis": {"type": "category", "data": [str(y) for y in y_cats]},
        "visualMap": {
            "min": float(df[v_col].min()) if len(df) > 0 else 0,
            "max": float(df[v_col].max()) if len(df) > 0 else 1,
            "calculable": True,
            "orient": "horizontal",
            "left": "center",
            "bottom": 15,
        },
        "series": [
            {
                "name": fields.series_name or "",
                "type": "heatmap",
                "data": data,
            }
        ],
    }


def _is_category_type(values: list) -> bool:
    from datetime import datetime
    for v in values[:100]:
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return False
        if isinstance(v, str):
            try:
                datetime.fromisoformat(v)
                return False
            except (ValueError, TypeError):
                pass
    return True


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _safe_list(series: pd.Series) -> list:
    return series.where(pd.notna(series), None).tolist()


def _safe_value(val):
    if pd.isna(val):
        return 0
    if hasattr(val, "item"):
        return val.item()
    return val