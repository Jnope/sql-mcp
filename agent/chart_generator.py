import pandas as pd


async def generate_chart(chart_type: str, data: dict, title: str = "") -> dict:
    df = pd.DataFrame(data.get("rows", []), columns=data.get("columns", []))

    builders = {
        "line": _build_line_chart,
        "bar": _build_bar_chart,
        "pie": _build_pie_chart,
        "scatter": _build_scatter_chart,
        "heatmap": _build_heatmap_chart,
    }
    builder = builders.get(chart_type)
    if builder is None:
        raise ValueError(f"不支持的图表类型: {chart_type}")
    return builder(df, title)


def _build_line_chart(df: pd.DataFrame, title: str) -> dict:
    x_field = df.columns[0]
    y_fields = [c for c in df.columns if c != x_field]
    return {
        "title": {"text": title},
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": _safe_list(df[x_field])},
        "yAxis": {"type": "value"},
        "series": [
            {"name": f, "type": "line", "data": _safe_list(df[f])}
            for f in y_fields
        ],
    }


def _build_bar_chart(df: pd.DataFrame, title: str) -> dict:
    x_field = df.columns[0]
    y_fields = [c for c in df.columns if c != x_field]
    return {
        "title": {"text": title},
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": _safe_list(df[x_field])},
        "yAxis": {"type": "value"},
        "series": [
            {"name": f, "type": "bar", "data": _safe_list(df[f])}
            for f in y_fields
        ],
    }


def _build_pie_chart(df: pd.DataFrame, title: str) -> dict:
    name_field = df.columns[0]
    value_field = df.columns[1] if len(df.columns) > 1 else df.columns[0]
    return {
        "title": {"text": title},
        "tooltip": {"trigger": "item"},
        "series": [
            {
                "type": "pie",
                "data": [
                    {"name": str(row[name_field]), "value": _safe_value(row[value_field])}
                    for _, row in df.iterrows()
                ],
            }
        ],
    }


def _build_scatter_chart(df: pd.DataFrame, title: str) -> dict:
    x_field = df.columns[0]
    y_field = df.columns[1] if len(df.columns) > 1 else df.columns[0]
    return {
        "title": {"text": title},
        "tooltip": {"trigger": "item"},
        "xAxis": {"type": "value", "name": str(x_field)},
        "yAxis": {"type": "value", "name": str(y_field)},
        "series": [
            {
                "type": "scatter",
                "data": df[[x_field, y_field]].where(pd.notna(df), None).values.tolist(),
            }
        ],
    }


def _build_heatmap_chart(df: pd.DataFrame, title: str) -> dict:
    x_field = df.columns[0]
    y_field = df.columns[1] if len(df.columns) > 1 else df.columns[0]
    value_field = df.columns[2] if len(df.columns) > 2 else df.columns[0]
    x_categories = df[x_field].unique().tolist()
    y_categories = df[y_field].unique().tolist()
    data = []
    for _, row in df.iterrows():
        x_idx = x_categories.index(row[x_field])
        y_idx = y_categories.index(row[y_field])
        data.append([x_idx, y_idx, _safe_value(row[value_field])])
    return {
        "title": {"text": title},
        "tooltip": {"trigger": "item"},
        "xAxis": {"type": "category", "data": [str(x) for x in x_categories]},
        "yAxis": {"type": "category", "data": [str(y) for y in y_categories]},
        "visualMap": {
            "min": float(df[value_field].min()) if len(df) > 0 else 0,
            "max": float(df[value_field].max()) if len(df) > 0 else 1,
            "calculable": True,
            "orient": "horizontal",
            "left": "center",
            "bottom": "15",
        },
        "series": [{"type": "heatmap", "data": data}],
    }


def _safe_list(series: pd.Series) -> list:
    return series.where(pd.notna(series), None).tolist()


def _safe_value(val):
    if pd.isna(val):
        return 0
    if hasattr(val, "item"):
        return val.item()
    return val
