---
name: sql-agent
description: |
  量化数据库Timelyre的智能SQL生成与执行MCP工具；
  支持自然语言查询数据库、Python数据加工、ECharts图表生成；
  触发词：查询数据、数据获取、生成SQL、SQL查询
license: MIT
compatibility: opencode
metadata:
  category: data
  vector_store: pgvector
---

# SQL Agent — 智能SQL生成与执行

通过自然语言查询金融数据库，自动检索表结构 → 生成SQL → 安全校验 → 只读执行 → 可选数据加工与图表生成。

## 意图 → 工具映射

| 用户意图        | 工具 | 说明                            |
|-------------|------|-------------------------------|
| 自然语言生成SQL   | `generate_and_execute_sql` | 生成SQL并执行，返回结果和AI结论            |
| 基于上次SQL生成图表 | `generate_echarts_from_last` | 重执行上次SQL（去LIMIT）生成ECharts配置   |
| 指定SQL生成图表   | `generate_echarts_from_sql` | 执行指定SQL并生成ECharts配置           |
| 获取schema列表  | `list_schemas` | 返回所有可用schema                  |
| 获取数据库列表     | `list_databases` | 根据schema创建连接，执行show databases |
| 数据处理/计算/统计  | `execute_python` | pandas/numpy 加工数据             |
| 刷新索引/重建向量   | `refresh_vector_index` | 单表或全量刷新                       |

模糊表述（如"看看数据"）必须询问确认意图，不得自行选择。

## 执行流程：自然语言生成SQL -> 查数据 → 图表

1. 调用 `generate_and_execute_sql(question)` 生成SQL并执行
2. 若需图表，调用 `generate_echarts_from_last(question, chart_type, title)` 基于上次结果生成配置

## 执行流程：自定义SQL → 图表

1. 调用 `list_schemas()` 获取可用schema列表
2. 调用 `list_databases(schema_name)` 获取该schema下数据库列表
3. 调用 `generate_echarts_from_sql(question, sql, schema_name, db, chart_type, title)` 执行SQL并生成图表

## 调用行为约束（严格遵守）

1. **禁止直接执行用户提供的SQL**：必须通过 `generate_echarts_from_sql` 工具执行
2. **只读查询**：仅允许 SELECT，禁止任何 DDL/DML
3. **等待完整返回**：调用工具后静默等待结果，不得中断或重试
4. **不自动重试**：无论是否失败，不自动重试
5. **大数据量提示**：结果超过 1000 行时提示用户缩小查询范围

## 数据输出约束

1. `generate_and_execute_sql`,`generate_echarts_from_last`和`generate_echarts_from_sql` 调用结束后，向用户反馈分析结束即可，**无须输出或解析对应结果**
2. 错误时结果结果中包含 `error` 属性值，需将原因展示给用户
