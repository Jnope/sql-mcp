import json
import logging
import pandas as pd
from openai import AsyncOpenAI
from common.config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
from .models.chart_model import NewChartFields

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是ECharts配置专家。根据用户需求、图表类型和数据列信息，判断数据是否适合可视化。

如果数据适合生成图表，调用 generate_echarts_option 工具生成配置。
如果数据不适合可视化（如单列无关联数据、数据量不足等），调用 reject_chart 工具说明原因。

generate_echarts_option 工具参数说明:
- title: 图表标题，根据数据内容智能生成
- chart_type: line/bar/pie/scatter/heatmap/candlestick/mixed
- 简单单系列场景: 用 x 和 y 指定列名
- K线图(candlestick): 用 x 指定日期列，series配置open_field/close_field/low_field/high_field
- 多系列/混合图表场景: 用 series 数组，每个系列独立配置 x_field/y_field/type/y_axis_index/stack
- group_field: 按某列值自动分组生成多系列
- y_axes: 双Y轴配置，当不同系列量纲差异大时使用
- visual_map_field: 散点图控制气泡大小/颜色，热力图控制颜色深浅
- 所有列名字段**必须**使用数据中的实际列名，不能翻译或自定义

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
        "parameters": NewChartFields.model_json_schema(),
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
        max_tokens=2048,
        tools=[GENERATE_TOOL, REJECT_TOOL],
        tool_choice="auto",
    )

    msg = resp.choices[0].message
    if not msg.tool_calls or len(msg.tool_calls) == 0:
        reason = msg.content or "LLM未调用任何工具"
        return {"error": f"数据不适合生成图表: {reason}"}

    if len(msg.tool_calls) != 1:
        names = [tc.function.name for tc in msg.tool_calls]
        return {"error": f"LLM应只调用一次工具，实际调用了 {len(msg.tool_calls)} 次: {names}"}

    tool_call = msg.tool_calls[0]
    tool_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)

    if tool_name == "reject_chart":
        return {"error": f"数据不适合生成图表: {args.get('reason', str(args) or '未知原因')}"}

    if tool_name != "generate_echarts_option":
        return {"error": f"LLM调用了未预期的工具: {tool_name}"}

    fields = NewChartFields(**args)

    df = pd.DataFrame(rows, columns=columns)
    option = _build_option(fields, df, title)

    return option


def _collect_used_columns(fields: NewChartFields) -> list[str]:
    """收集 fields 中引用的所有列名，用于统一校验。"""
    cols = []
    if fields.x:
        cols.append(fields.x)
    if fields.y:
        cols.append(fields.y)
    if fields.group_field:
        cols.append(fields.group_field)
    if fields.visual_map_field:
        cols.append(fields.visual_map_field)
    for sc in fields.series:
        if sc.x_field:
            cols.append(sc.x_field)
        if sc.y_field:
            cols.append(sc.y_field)
        if sc.open_field:
            cols.append(sc.open_field)
        if sc.close_field:
            cols.append(sc.close_field)
        if sc.low_field:
            cols.append(sc.low_field)
        if sc.high_field:
            cols.append(sc.high_field)
    return list(set(cols))


def _build_option(fields: NewChartFields, df: pd.DataFrame, title: str) -> dict:
    used_cols = _collect_used_columns(fields)
    missing = [c for c in used_cols if c not in df.columns]
    if missing:
        return {"error": f"列名 {missing} 不在数据列 {list(df.columns)} 中"}

    chart_type = fields.chart_type.value if hasattr(fields.chart_type, "value") else str(fields.chart_type)

    if chart_type == "pie":
        option = _build_pie(fields, df, title)
    elif chart_type == "scatter":
        option = _build_scatter(fields, df, title)
    elif chart_type == "heatmap":
        option = _build_heatmap(fields, df, title)
    elif chart_type == "candlestick":
        option = _build_candlestick(fields, df, title)
    else:
        option = _build_line_bar(fields, df, title)

    if "error" in option:
        return option

    option["title"] = {"text": title or fields.title or ""}

    if fields.extra_config:
        option = _deep_merge(option, fields.extra_config)

    return option


def _resolve_series(fields: NewChartFields, df: pd.DataFrame) -> list[dict]:
    """从 fields 解析出统一的 series 配置列表。

    优先级: series 数组 > group_field 自动分组 > x/y 简单模式

    Returns:
        list[dict]: 每项含 name, type, x_field, y_field, y_axis_index, stack
    """

    if fields.series:
        return [
            {
                "name": sc.name,
                "type": sc.type.value if hasattr(sc.type, "value") else str(sc.type),
                "x_field": sc.x_field or fields.x or "",
                "y_field": sc.y_field or fields.y or "",
                "open_field": sc.open_field,
                "close_field": sc.close_field,
                "low_field": sc.low_field,
                "high_field": sc.high_field,
                "y_axis_index": sc.y_axis_index,
                "stack": sc.stack,
            }
            for sc in fields.series
        ]

    if fields.group_field:
        group_values = df[fields.group_field].unique().tolist()
        chart_type = fields.chart_type.value if hasattr(fields.chart_type, "value") else str(fields.chart_type)
        return [
            {
                "name": str(g),
                "type": chart_type if chart_type != "mixed" else "line",
                "x_field": fields.x,
                "y_field": fields.y,
                "y_axis_index": 0,
                "stack": None,
            }
            for g in group_values
        ]

    chart_type = fields.chart_type.value if hasattr(fields.chart_type, "value") else str(fields.chart_type)
    return [
        {
            "name": fields.y or "",
            "type": chart_type if chart_type != "mixed" else "line",
            "x_field": fields.x,
            "y_field": fields.y,
            "y_axis_index": 0,
            "stack": None,
        }
    ]


def _build_line_bar(fields: NewChartFields, df: pd.DataFrame, title: str) -> dict:
    series_configs = _resolve_series(fields, df)
    x_col = series_configs[0]["x_field"] or df.columns[0]

    x_raw = _safe_list(df[x_col])
    x_is_category = _is_category_type(x_raw)

    series = []
    for sc in series_configs:
        s = {"name": sc["name"], "type": sc["type"]}
        if fields.group_field:
            mask = df[fields.group_field].astype(str) == sc["name"]
            sub_df = df[mask]
            if x_is_category:
                s["data"] = _safe_list(sub_df[sc["y_field"]])
            else:
                s["data"] = sub_df[[sc["x_field"], sc["y_field"]]].values.tolist()
        else:
            if x_is_category:
                s["data"] = _safe_list(df[sc["y_field"]])
            else:
                s["data"] = df[[sc["x_field"], sc["y_field"]]].values.tolist()
        if sc.get("y_axis_index"):
            s["yAxisIndex"] = sc["y_axis_index"]
        if sc.get("stack"):
            s["stack"] = sc["stack"]
        series.append(s)

    if fields.y_axes:
        y_axis = [
            {"type": "value", "name": ya.name, "position": ya.position.value if hasattr(ya.position, "value") else str(ya.position)}
            for ya in fields.y_axes
        ]
    else:
        y_axis = {"type": "value"}

    result = {
        "title": {"text": title or fields.title or ""},
        "tooltip": {"trigger": "axis"},
        "legend": {},
        "xAxis": {"type": "category" if x_is_category else "time", "name": x_col},
        "yAxis": y_axis,
        "series": series,
    }
    if x_is_category:
        result["xAxis"]["data"] = x_raw

    return result


def _build_candlestick(fields: NewChartFields, df: pd.DataFrame, title: str) -> dict:
    series_configs = _resolve_series(fields, df)
    sc = series_configs[0]
    x_col = sc["x_field"] or fields.x or df.columns[0]

    open_c = sc.get("open_field") or df.columns[1]
    close_c = sc.get("close_field") or df.columns[2]
    low_c = sc.get("low_field") or df.columns[3]
    high_c = sc.get("high_field") or df.columns[4]
    missing = [c for c in [x_col, open_c, close_c, low_c, high_c] if c not in df.columns]
    if missing:
        return {"error": f"K线图列名 {missing} 不在数据列 {list(df.columns)} 中"}

    x_raw = _safe_list(df[x_col])
    x_is_category = _is_category_type(x_raw)

    if x_is_category:
        data = df[[open_c, close_c, low_c, high_c]].values.tolist()
    else:
        data = df[[x_col, open_c, close_c, low_c, high_c]].values.tolist()

    result = {
        "title": {"text": title or fields.title or ""},
        "tooltip": {"trigger": "axis"},
        "legend": {},
        "xAxis": {"type": "category" if x_is_category else "time", "name": x_col},
        "yAxis": {"type": "value",
            "scale": True,
            "splitArea": {
                "show": True
            }
        },
        "series": [
            {
                "name": sc["name"] or "K线",
                "type": "candlestick",
                "data": data,
            }
        ],
    }
    if x_is_category:
        result["xAxis"]["data"] = x_raw

    return result


def _build_pie(fields: NewChartFields, df: pd.DataFrame, title: str) -> dict:
    name_col = fields.x or df.columns[0]
    value_col = fields.y or df.columns[1]

    data = [
        {"name": _safe_value(row[name_col]), "value": _safe_value(row[value_col])}
        for _, row in df.iterrows()
    ]

    return {
        "title": {"text": title or fields.title or ""},
        "tooltip": {"trigger": "item"},
        "legend": {"orient": "vertical", "left": "left"},
        "series": [
            {
                "name": value_col,
                "type": "pie",
                "radius": "60%",
                "data": data,
            }
        ],
    }


def _build_scatter(fields: NewChartFields, df: pd.DataFrame, title: str) -> dict:
    series_configs = _resolve_series(fields, df)

    series = []
    for sc in series_configs:
        data = df[[sc["x_field"], sc["y_field"]]].where(pd.notna(df[[sc["x_field"], sc["y_field"]]]), None).values.tolist()
        s = {"name": sc["name"], "type": "scatter", "data": data}
        series.append(s)

    x_name = series_configs[0]["x_field"]
    y_name = series_configs[0]["y_field"]

    result = {
        "title": {"text": title or fields.title or ""},
        "tooltip": {"trigger": "item"},
        "xAxis": {"type": "value", "name": x_name},
        "yAxis": {"type": "value", "name": y_name},
        "series": series,
    }

    if fields.visual_map_field and fields.visual_map_field in df.columns:
        vmin = float(df[fields.visual_map_field].min())
        vmax = float(df[fields.visual_map_field].max())
        result["visualMap"] = {
            "min": vmin,
            "max": vmax,
            "dimension": 2,
            "calculable": True,
            "orient": "horizontal",
            "left": "center",
            "bottom": 15,
        }

    return result


def _build_heatmap(fields: NewChartFields, df: pd.DataFrame, title: str) -> dict:
    x_col = fields.x or df.columns[0]
    y_col = fields.y or df.columns[1]
    v_col = fields.visual_map_field or df.columns[2]

    x_cats = df[x_col].unique().tolist()
    y_cats = df[y_col].unique().tolist()

    data = []
    for _, row in df.iterrows():
        x_idx = x_cats.index(row[x_col])
        y_idx = y_cats.index(row[y_col])
        data.append([x_idx, y_idx, _safe_value(row[v_col])])

    return {
        "title": {"text": title or fields.title or ""},
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
                "name": v_col,
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