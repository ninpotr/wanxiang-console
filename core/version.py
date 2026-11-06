"""代码版本戳。

解决的问题：改完前后端代码后，用户浏览器里跑的还可能是旧版 JS。
旧 JS 配上新服务端，轻则报错，重则提交出错误的参数，而且**用户看不出来**。

做法：把 web/ 和服务端 .py 文件的「修改时间 + 大小」算成一个指纹。
页面加载时记一份，之后定期比对；变了就提示刷新。

用 mtime+size 而不是内容哈希：文件小、变化频繁，mtime 足够且几乎零开销。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 这些目录/文件变了就说明代码变了
WATCHED = (
    ROOT / "app.py",
    ROOT / "web",
    ROOT / "core",
)


def _entries():
    for target in WATCHED:
        if target.is_file():
            yield target
        elif target.is_dir():
            for path in target.rglob("*"):
                if path.is_file() and path.suffix in (".py", ".js", ".css", ".html"):
                    yield path


def code_stamp() -> str:
    """返回代码指纹。任何一个被监视的文件变了，指纹就变。"""
    digest = 0
    count = 0
    for path in sorted(_entries(), key=lambda p: str(p)):
        try:
            stat = path.stat()
        except OSError:
            continue
        # 简单地把 mtime 和大小混起来，够用且快
        digest = (digest * 1_000_003 + stat.st_mtime_ns + stat.st_size) % (2**63)
        count += 1
    return f"{digest:016x}-{count}"


def web_stamp() -> str:
    """只算前端资源。前端变了才需要用户刷新页面。"""
    digest = 0
    count = 0
    web = ROOT / "web"
    if not web.is_dir():
        return "0-0"
    for path in sorted(web.rglob("*"), key=lambda p: str(p)):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        digest = (digest * 1_000_003 + stat.st_mtime_ns + stat.st_size) % (2**63)
        count += 1
    return f"{digest:016x}-{count}"
