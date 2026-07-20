
## 环境变量

| 变量                    | 说明                   | 默认值                           |
|-----------------------|----------------------|-------------------------------|
| LOG_LEVEL             | 日志等级                 | WARNING                       |
| LOG_DIR               | 日志位置                 | $HOME/.sql/logs               |
| API_HOST              | api host             | 0.0.0.0                       |
| TIMELYRE_PROXY_XXX    | quark proxy          | TIMELYRE_PROXY                |
| TIMELYRE_CONN_XXX     | 连接信息                 | TIMELYRE_CONN                 |
| TIMELYRE_USER_XXX     | 用户                   | TIMELYRE_USER                 |
| TIMELYRE_PASSWORD_XXX | 密码                   | TIMELYRE_PASSWORD             |
| TIMELYRE_TOKEN_XXX    | guarian token        | TIMELYRE_TOKEN                |
| SYNC_SCHEMAS          | 数据库实例，,分隔            | quark1,quark2                 |
| SYNC_EXCLUDE_DBS      | 排除的数据库，,分隔           | default,timelyre_cache,system |
| SYNC_INTERVAL_HOURS   | 定时任务间隔               | 24                            |
| EMBEDDING_API_URL     | ollama embedding url | -                             |
| EMBEDDING_MODEL       | rag 模型               | bge-m3                        |
| EMBEDDING_API_KEY     | API key              | not-needed                    |
| MCP_CLOSE_PROXY       | 是否关闭系统代理             | false                         |


## 安装

```shell
# 依赖安装
uv sync --extra mcp --extra api

# wheel 构建
uv build --wheel

# 安装
pipx install --python python3.12 --pip pip /opt/upgrade/sql_agent-0.1.0-py3-none-any.whl[mcp] \
 --force --pip-args="--find-links /opt/upgrade/" --backend pip
或安装 sql_agent-0.1.0-py3-none-any.whl[api]

# 执行
sql-agent-mcp 启动fastmcp
sql-agent-api 启动fastapi服务
```
