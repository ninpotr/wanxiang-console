"""运行日志。

为什么要做这个：出问题时没有痕迹可查是最难受的。尤其是 request_id ——
官方文档写明「建议在调用失败时记录该值，便于自助排查或提交工单时提供」，
而它默认只存在于一次 HTTP 响应里，不记就没了。

日志里**绝不出现 API Key**：写入前统一过一遍 redact()，即使将来有代码
不小心把整个 header 塞进来，也会被抹掉。
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "wan3.log"

MAX_BYTES = 2 * 1024 * 1024   # 单文件 2MB
BACKUP_COUNT = 5              # 连同当前文件共保留 6 个，约 12MB 上限

# sk- 开头的一律当密钥抹掉。宁可误伤也不要漏。
KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")
# 极少数情况下密钥可能带在 URL query 里
QUERY_KEY_PATTERN = re.compile(r"(api[_-]?key=)[^&\s]+", re.IGNORECASE)

LOGGER_NAME = "wan3"
_configured = False


def redact(text: object) -> str:
    """抹掉文本里可能的密钥。所有写日志的入口都走这里。"""
    value = str(text)
    value = KEY_PATTERN.sub("sk-***已隐去***", value)
    value = QUERY_KEY_PATTERN.sub(r"\1***已隐去***", value)
    return value


class _RedactingFormatter(logging.Formatter):
    """兜底：格式化后再过一遍 redact，防止拼接出来的消息漏掉密钥。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging() -> logging.Logger:
    """初始化日志。重复调用只生效一次。"""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)

    if _configured:
        return logger

    LOG_DIR.mkdir(exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = _RedactingFormatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

    # requests / urllib3 自己的日志太吵，而且会把完整 header 打出来
    for noisy in ("urllib3", "requests", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    return logger


def get_logger() -> logging.Logger:
    return setup_logging()


def log_http(
    logger: logging.Logger,
    method: str,
    url: str,
    status: int | None,
    *,
    request_id: str | None = None,
    elapsed: float | None = None,
    task_id: str | None = None,
    error: str | None = None,
    body: str | None = None,
) -> None:
    """记录一次接口调用。

    request_id 单独成段，方便出问题时直接从这里复制给工单。
    """
    short_url = url.split("?")[0]
    # 去掉域名，日志里只留路径，省得每行都很长
    if "://" in short_url:
        short_url = "/" + short_url.split("://", 1)[1].split("/", 1)[-1]

    parts = [f"{method} {short_url}"]
    parts.append(f"-> {status}" if status is not None else "-> 未响应")
    if elapsed is not None:
        parts.append(f"{elapsed:.2f}s")
    if task_id:
        parts.append(f"task_id={task_id}")
    if request_id:
        parts.append(f"request_id={request_id}")
    if error:
        parts.append(f"错误={error}")
    if body:
        parts.append(f"响应={body[:500]}")

    level = logging.ERROR if (error or (status is not None and status >= 400)) else logging.INFO
    logger.log(level, " ".join(parts))


def tail(lines: int = 400) -> list[str]:
    """读日志尾部，给界面用。含轮转出来的旧文件则按顺序拼接。"""
    if not LOG_FILE.exists():
        return []
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.readlines()
    except OSError:
        return []
    return [line.rstrip("\n") for line in content[-lines:]]


def log_files() -> list[Path]:
    """当前所有日志文件，新的在前。"""
    if not LOG_DIR.exists():
        return []
    return sorted(LOG_DIR.glob("wan3.log*"), key=lambda p: p.stat().st_mtime, reverse=True)
