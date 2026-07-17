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
| 基于上次SQL生成图表 | `generate_echarts_from_last` | 重执行上次SQL（去LIMIT）生成ECharts配置 |
| 指定SQL生成图表 | `generate_echarts_from_sql` | 执行指定SQL并生成ECharts配置 |
| 获取schema列表 | `list_schemas` | 返回所有可用schema |
| 获取数据库列表 | `list_databases` | 根据schema创建连接，执行show databases |
| 数据处理/计算/统计 | `execute_python` | pandas/numpy 加工数据 |
| 刷新索引/重建向量 | `refresh_vector_index` | 单表或全量刷新 |

模糊表述（如"看看数据"）必须询问确认意图，不得自行选择。

## 执行流程：自然语言查数据 → 图表

1. 调用 `search_tables(question)` 向量检索相关表结构
2. 调用 `generate_and_execute_sql(question)` 生成SQL并执行
3. 若需图表，调用 `generate_echarts_from_last(question, chart_type, title)` 基于上次结果生成配置

## 执行流程：自定义SQL → 图表

1. 调用 `list_schemas()` 获取可用schema列表
2. 调用 `list_databases(schema_name)` 获取该schema下数据库列表
3. 调用 `generate_echarts_from_sql(question, sql, schema_name, db, chart_type, title)` 执行SQL并生成图表

或者直接：
1. 调用 `list_schemas()` + `list_databases(schema_name)` 确认schema和db
2. 调用 `generate_echarts_from_sql(...)` 一步完成SQL执行和图表生成

## 调用行为约束（严格遵守）

1. **禁止直接执行用户提供的SQL**：必须通过 `generate_and_execute_sql` 或 `generate_echarts_from_sql` 工具执行
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
2. `generate_and_execute_sql` 返回的 `result` 结构含 `columns`、`rows`、`rowCount`，结果限制最多100行（`truncated`字段标识是否截断，`totalRowCount`为实际总行数）
3. `generate_echarts_from_last` / `generate_echarts_from_sql` 返回的 JSON 可直接用于前端 ECharts 渲染（不写文件，直接返回配置）
4. 图表工具中 LLM 判断数据不适合可视化时返回 `{"error": "原因说明"}`，需将原因展示给用户
5. `list_schemas` 返回 `{"schemas": [...]}`，`list_databases` 返回 `{"databases": [...]}`
6. 结果中 `explanation` 字段为 AI 生成的数据结论，可展示给用户

## 返回数据结构声明（仅供校验，禁止拆解）

### generate_and_execute_sql

- `sql` (str) — 生成的SQL语句
- `result` (object) — 查询结果
  - `columns` (list[str]) — 列名
  - `rows` (list[list]) — 数据行（最多100行）
  - `rowCount` (int) — 返回行数
  - `truncated` (bool) — 是否因行数限制被截断
  - `totalRowCount` (int) — 实际总行数
- `schema_name` (str) — schema名称
- `db` (str) — 数据库名称
- `explanation` (str) — AI数据结论
- `error` (str) — 错误信息（如有）

### generate_echarts_from_last / generate_echarts_from_sql

- 成功: ECharts配置字典（含 title/tooltip/legend/xAxis/yAxis/series/chart_type 等）
- 失败: `{"error": "原因说明"}` — 数据不适合可视化或执行失败时的原因

### list_schemas

- `schemas` (list[str]) — schema名称列表
- `error` (str) — 错误信息（如有）

### list_databases

- `databases` (list[str]) — 数据库名称列表
- `error` (str) — 错误信息（如有）
