import os

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
os.environ.pop('http_proxy', None)
os.environ.pop('https_proxy', None)
os.environ.setdefault('NO_PROXY', "*")

PG_DSN = os.environ.get(
    "PGVECTOR_DSN",
    "host=172.18.192.76 port=16543 dbname=sqlagent user=postgres password=postgres",
)

EMBEDDING_API_URL = os.environ.get(
    "EMBEDDING_API_URL", "http://172.18.192.76:11434/v1/embeddings"
)
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "bge-m3")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "not-needed")

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://llmops.transwarp.io/vibecoding/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "llmops-zhenjiang-368c5e8878cf7b0f55b02401fab49aec")
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/deepseek-v4-flash")

RETRIEVE_TOP_N = int(os.environ.get("RETRIEVE_TOP_N", "10"))

TIMELYRE_PROXY = os.environ.get("TIMELYRE_PROXY", "172.18.192.74:9998")
TIMELYRE_CONN = os.environ.get("TIMELYRE_CONN", "jdbc:hive2://172.18.192.75:10006")
TIMELYRE_DEFAULT_DB = os.environ.get("TIMELYRE_DB", "default")
TIMELYRE_USER = os.environ.get("TIMELYRE_USER", "admin")
TIMELYRE_PASSWORD = os.environ.get("TIMELYRE_PASSWORD", "admin")
TIMELYRE_TOKEN = os.environ.get("TIMELYRE_TOKEN", "UgJRRGe7qMAKcirOQ017-TDH")
TIMELYRE_SESSION_TIMEOUT = int(os.environ.get("TIMELYRE_SESSION_TIMEOUT", "60000"))
TIMELYRE_LOGIN_TIMEOUT = int(os.environ.get("TIMELYRE_LOGIN_TIMEOUT", "15000"))

MAX_RETURN_ROWS = int(os.environ.get("MAX_RETURN_ROWS", "100"))
MAX_CHART_ROWS = int(os.environ.get("MAX_CHART_ROWS", "10000"))
AVAILABLE_SCHEMAS = os.environ.get("AVAILABLE_SCHEMAS", "")

CLOSE_ENV = os.getenv("MCP_CLOSE_PROXY", "false")
