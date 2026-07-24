from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ChartType(str, Enum):
    LINE = "line"
    BAR = "bar"
    PIE = "pie"
    SCATTER = "scatter"
    HEATMAP = "heatmap"
    CANDLESTICK = "candlestick"
    MIXED = "mixed"

class SeriesType(str, Enum):
    LINE = "line"
    BAR = "bar"
    SCATTER = "scatter"
    CANDLESTICK = "candlestick"

class AxisPosition(str, Enum):
    LEFT = "left"
    RIGHT = "right"

class SeriesConfig(BaseModel):
    """单个数据系列的配置。当需要多个系列（如多条折线）或混合图表类型（如折线+柱状图）时使用"""

    name: str = Field(
        description="系列名称，显示在图例中。根据y_field字段含义生成，如'温度'、'销量'"
    )
    type: SeriesType = Field(
        description="该系列的图表类型。可与chart_type不同以实现混合图表，如折线+柱状图组合"
    )
    x_type: str = Field(
        default="category",
        description="该系列x轴数据类型。'category'=类别型(柱状图等)，'time'=时间序列(k线图等)。留空默认为category"
    )
    x_field: str = Field(
        description="该系列x轴使用的列名。必须使用数据中的实际列名，不能翻译。如'日期'、'月份'、'时间'"
    )
    y_field: str = Field(
        description="该系列y轴使用的列名。必须使用数据中的实际列名，不能翻译。如'温度'、'销售额'、'数量'"
    )
    open_field: Optional[str] = Field(
        default=None,
        description="K线图开盘价列名。仅chart_type=candlestick时使用。必须使用数据中的实际列名"
    )
    close_field: Optional[str] = Field(
        default=None,
        description="K线图收盘价列名。仅chart_type=candlestick时使用。必须使用数据中的实际列名"
    )
    low_field: Optional[str] = Field(
        default=None,
        description="K线图最低价列名。仅chart_type=candlestick时使用。必须使用数据中的实际列名"
    )
    high_field: Optional[str] = Field(
        default=None,
        description="K线图最高价列名。仅chart_type=candlestick时使用。必须使用数据中的实际列名"
    )
    y_axis_index: int = Field(
        default=0,
        description="使用哪个Y轴。0=左轴，1=右轴。当不同系列数值量纲差异大时(如温度0-40和销量0-10000)，使用不同轴"
    )
    stack: Optional[str] = Field(
        default=None,
        description="堆叠组名。相同stack值的系列会堆叠显示。如'总量'，让多个系列叠加"
    )

class YAxisConfig(BaseModel):
    """Y轴配置。仅在需要双轴（量纲不同）或多轴时填写"""

    name: str = Field(
        description="轴名称，显示在轴旁。如'温度(℃)'、'销售额(万元)'"
    )
    position: AxisPosition = Field(
        description="轴位置。left=左轴，right=右轴"
    )

class ChartFields(BaseModel):
    title: str = ""
    chart_type: str
    x_axis: str | None = None
    y_axis_fields: list[str] | None = None
    name_field: str | None = None
    value_field: str | None = None
    x_value_field: str | None = None
    y_value_field: str | None = None
    x_category_field: str | None = None
    y_category_field: str | None = None
    value_heatmap_field: str | None = None
    series_name: str | None = None
    extra_config: dict[str, Any] = {}
    data_mapping: dict[str, Any] = {}

class NewChartFields(BaseModel):
    """根据数据生成ECharts图表的配置。包含图表类型、数据列映射、系列配置等信息。

    重要规则：
    - 所有列名字段(x、y、x_field、y_field等)必须使用数据中的实际列名，不能翻译或修改
    - 简单单系列场景使用x/y，复杂多系列场景使用series数组，两者互斥
    - 当多个指标数值范围差异过大时，使用双Y轴避免小数值被压缩
    """

    title: str = Field(
        default="",
        description="图表标题。根据数据内容智能生成，如'各月份销售趋势'、'温度与销量关系'"
    )
    chart_type: ChartType = Field(
        description="图表整体类型。判断依据：时间趋势→line，类别对比→bar，占比分析→pie，相关性→scatter，热力分布→heatmap，多种图表叠加→mixed"
    )
    x_type: str = Field(
        default="category",
        description="x轴数据类型。'category'=类别型(柱状图等)，'time'=时间序列(k线图等)。留空默认为category"
    )

    # 简单模式（单系列）
    x: Optional[str] = Field(
        default=None,
        description="简单模式下x轴使用的列名。如'日期'、'月份'。与series互斥"
    )
    y: Optional[str] = Field(
        default=None,
        description="简单模式下y轴使用的列名。如'销售额'。与series互斥"
    )

    # 多系列模式
    series: list[SeriesConfig] = Field(
        default_factory=list,
        description="多系列配置数组。适用场景：1)多条折线/柱状图 2)混合图表(折线+散点) 3)需要双Y轴。每个系列独立配置类型和轴"
    )

    # 自动分组
    group_field: Optional[str] = Field(
        default=None,
        description="分组字段名。按此列的不同值自动生成多个系列。如按'地区'分组，每个地区一条线。与series互斥"
    )

    # 双轴配置
    y_axes: Optional[list[YAxisConfig]] = Field(
        default=None,
        description="多Y轴配置。当series中使用y_axis_index=1时需要定义。如[{'name':'温度','position':'left'},{'name':'销量','position':'right'}]"
    )

    # 视觉映射
    visual_map_field: Optional[str] = Field(
        default=None,
        description="用于视觉映射的字段。散点图中控制气泡大小/颜色，热力图中控制颜色深浅。如'销量'、'占比'"
    )

    # 提示信息
    tooltip_fields: Optional[list[str]] = Field(
        default=None,
        description="鼠标悬停时额外显示的字段。如['地区','负责人']，让tooltip显示更多信息"
    )

    # 额外配置
    extra_config: dict[str, Any] = Field(
        default_factory=dict,
        description="额外的ECharts配置项，如legend位置、tooltip格式等，会深度合并到最终配置"
    )

class EChartsOption(BaseModel):
    title: dict[str, Any] | None = None
    tooltip: dict[str, Any] | None = {"trigger": "axis"}
    legend: dict[str, Any] | None = None
    xAxis: dict[str, Any] | list[dict[str, Any]] | None = {}
    yAxis: dict[str, Any] | list[dict[str, Any]] | None = {}
    series: list[dict[str, Any]]
    visualMap: dict[str, Any] | None = None
