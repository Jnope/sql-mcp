import logging
import os
import sys
from pathlib import Path


def setup_logging():
    from logging.handlers import TimedRotatingFileHandler

    level_str = os.getenv("LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_str, logging.WARNING)
    log_dir = os.getenv(
        "LOG_DIR",
        str(Path.home() / ".sql" / "logs"),
    )
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    fmt_console = "%(asctime)s %(levelname)s [%(name)s]: %(message)s"
    fmt_file = "%(asctime)s | %(levelname)s | %(name)s | %(module)s:%(funcName)s:%(lineno)d | %(message)s"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(logging.Formatter(fmt_console))
        root.addHandler(console)

    if not any(isinstance(h, TimedRotatingFileHandler) and getattr(h, '_baseFilename', '').endswith('sql_mcp.log') for h in root.handlers):
        fh = TimedRotatingFileHandler(
            filename=Path(log_dir) / "sql_mcp.log",
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(fmt_file))
        fh.suffix = "%Y-%m-%d"
        root.addHandler(fh)

    if not any(isinstance(h, TimedRotatingFileHandler) and getattr(h, '_baseFilename', '').endswith('error.log') for h in root.handlers):
        eh = TimedRotatingFileHandler(
            filename=Path(log_dir) / "error.log",
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
        )
        eh.setLevel(logging.WARNING)
        eh.setFormatter(logging.Formatter(fmt_file))
        eh.suffix = "%Y-%m-%d"
        root.addHandler(eh)

    for name, lvl in [
        ("urllib3", logging.WARNING),
        ("requests", logging.WARNING),
        ("httpx", logging.WARNING),
        ("httpcore", logging.WARNING),
        ("matplotlib", logging.WARNING),
        ("database", logging.WARNING),
    ]:
        logging.getLogger(name).setLevel(lvl)


def setup_admin_logging():
    from logging.handlers import TimedRotatingFileHandler

    setup_logging()

    level_str = os.getenv("LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_str, logging.WARNING)
    log_dir = os.getenv(
        "LOG_DIR",
        str(Path.home() / ".sql" / "logs"),
    )
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    fmt_file = "%(asctime)s | %(levelname)s | %(name)s | %(module)s:%(funcName)s:%(lineno)d | %(message)s"

    root = logging.getLogger()

    if not any(isinstance(h, TimedRotatingFileHandler) and getattr(h, '_baseFilename', '').endswith('sql_admin.log') for h in root.handlers):
        fh = TimedRotatingFileHandler(
            filename=Path(log_dir) / "sql_admin.log",
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(fmt_file))
        fh.suffix = "%Y-%m-%d"
        root.addHandler(fh)