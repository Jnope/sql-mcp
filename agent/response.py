from typing import Any, Dict

_SERVER_NAME = "SQLTool"


def build_response(
    *,
    tool: str,
    question: str,
    success: bool = True,
    error: str = "",
    elapsed_seconds: float = 0.0,
    data: Any = None,
) -> dict:
    ctx: Dict[str, Any] = {
        "server_name": _SERVER_NAME,
        "tool": tool,
        "elapsed_seconds": elapsed_seconds,
        "question": question,
    }

    return {
        "success": success,
        "error": error,
        "ctx": ctx,
        "data": data,
    }

def _sanitize_quotes(text: str) -> str:
    return text.replace('"', "'").replace("\u201c", "\u300c").replace("\u201d", "\u300d")
