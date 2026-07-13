---
name: sql-agent
description: |
  智能SQL生成与执行MCP工具，基于pgvector向量检索表结构、LLM生成SQL、AST安全校验、只读执行；
  支持自然语言查询数据库、Python数据加工、ECharts图表生成；
  触发词：查询数据、生成SQL、数据分析、数据可视化、查数据库、SQL查询
license: MIT
compatibility: opencode
metadata:
  category: data
  tools: sql-agent
  database: TimelyRe(Doris)
  vector_store: pgvector
---

# SQL Agent — 智能SQL生成与执行

通过自然语言查询金融数据库，自动检索表结构 → 生成SQL → 安全校验 → 只读执行 → 可选数据加工与图表生成。

## 意图 → 工具映射

| 用户意图 | 工具 | 说明 |
|---------|------|------|
| 查数据/查表/有哪些表 | `search_tables` | 向量检索相关表结构 |
| 自然语言查询/跑个SQL | `generate_and_execute_sql` | 生成SQL并执行，返回结果+AI结论 |
| 数据处理/计算/统计 | `execute_python` | pandas/numpy 加工数据 |
| 画图/图表/可视化 | `generate_echarts_config` | line/bar/pie/scatter/heatmap |
| 刷新索引/重建向量 | `refresh_vector_index` | 单表或全量刷新 |

模糊表述（如"看看数据"）必须询问确认意图，不得自行选择。

## 执行流程

1. 调用 `search_tables(question)` 向量检索相关表结构
2. 调用 `generate_and_execute_sql(question)` 生成SQL并执行
3. 若需数据加工，调用 `execute_python(code, data)` 用 pandas 处理
4. 若需图表，调用 `generate_echarts_config(chart_type, data, title)` 生成 ECharts 配置
5. 将结果以表格 + 图表 + 文字结论返回用户

## 调用行为约束（严格遵守）

1. **禁止直接执行用户提供的SQL**：必须通过 `generate_and_execute_sql` 走完整流程（检索→生成→校验→执行）
2. **只读查询**：仅允许 SELECT，禁止任何 DDL/DML
3. **等待完整返回**：调用工具后静默等待结果，不得中断或重试
4. **不自动重试**：无论是否失败，不自动重试
5. **大数据量提示**：结果超过 1000 行时提示用户缩小查询范围

## 图表类型选择规则

| 数据形态 | 默认图表 |
|---------|---------|
| 时间 + 数值 | 折线图 (line) |
| 分类 + 数值 | 柱状图 (bar) |
| 占比数据 | 饼图 (pie) |
| 两个连续变量 | 散点图 (scatter) |
| 股票 + 涨跌 | 热力图 (heatmap) |
| 不适合可视化 | 不自动生成 |

## 数据输出约束

1. 工具返回的 JSON 必须原样透传，禁止二次序列化
2. `generate_and_execute_sql` 返回的 `result` 结构含 `columns`、`rows`、`rowCount`
3. `generate_echarts_config` 返回的 JSON 可直接用于前端 ECharts 渲染
4. 结果中 `explanation` 字段为 AI 生成的数据结论，可展示给用户

## 返回数据结构声明（仅供校验，禁止拆解）

- `sql` (str) — 生成的SQL语句
- `result` (object) — 查询结果
  - `columns` (list[str]) — 列名
  - `rows` (list[list]) — 数据行
  - `rowCount` (int) — 行数
- `explanation` (str) — AI数据结论
- `error` (str) — 错误信息（如有）
