"""落盘：每个任务一个文件夹，视频 + 参数 + 素材副本一起存。

目录结构：
    outputs/20260101_1200_示例视频/
        video.mp4      生成的视频
        meta.json      参数、提示词、素材清单、实际用量与成本
        request.json   实际发给接口的请求体，可直接复现
        refs/          参考素材的本地副本

refs/ 存在的理由：官方临时 URL 48 小时就失效，且上传后不可下载。
不存副本的话，三天后想复现这条视频就再也找不到当初用的素材了。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ROOT

UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class StoreError(RuntimeError):
    pass


def output_root(settings: dict[str, Any]) -> Path:
    raw = settings.get("output_dir") or "outputs"
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def slugify(text: str, limit: int = 24) -> str:
    """把提示词开头转成安全的文件夹名片段（Windows 也适用）。"""
    cleaned = UNSAFE_CHARS.sub("", (text or "").strip())
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = cleaned.strip(". ")
    if not cleaned:
        return "未命名"
    if cleaned.upper() in RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:limit]


def new_job_dir(settings: dict[str, Any], prompt: str) -> Path:
    """按 时间_提示词片段 建任务目录，重名自动加序号。"""
    root = output_root(settings)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{stamp}_{slugify(prompt)}"

    candidate = root / base
    index = 2
    while candidate.exists():
        candidate = root / f"{base}_{index}"
        index += 1

    candidate.mkdir(parents=True)
    return candidate


def copy_reference(job_dir: Path, source: Path) -> str | None:
    """把参考素材复制进 refs/，返回相对路径。已存在则直接用。"""
    source = Path(source)
    if not source.exists():
        return None

    refs = job_dir / "refs"
    refs.mkdir(exist_ok=True)

    target = refs / source.name
    index = 2
    while target.exists():
        if target.stat().st_size == source.stat().st_size:
            return str(target.relative_to(job_dir))
        target = refs / f"{source.stem}_{index}{source.suffix}"
        index += 1

    try:
        shutil.copy2(source, target)
    except OSError:
        return None
    return str(target.relative_to(job_dir))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_meta(job_dir: Path, meta: dict[str, Any]) -> None:
    write_json(job_dir / "meta.json", meta)


def write_request(job_dir: Path, request: dict[str, Any]) -> None:
    write_json(job_dir / "request.json", request)


def read_meta(job_dir: Path) -> dict[str, Any] | None:
    meta_path = job_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_jobs(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """列出历史任务，最新的在前。"""
    root = output_root(settings)
    jobs: list[dict[str, Any]] = []

    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        meta = read_meta(entry)
        if meta is None:
            continue
        jobs.append(
            {
                "dir": entry.name,
                "created_at": meta.get("created_at"),
                "status": meta.get("status"),
                "prompt": (meta.get("prompt") or {}).get("final", ""),
                "original_prompt": (meta.get("prompt") or {}).get("original", ""),
                "model": meta.get("model"),
                "resolution": (meta.get("parameters") or {}).get("resolution"),
                "duration": (meta.get("parameters") or {}).get("duration"),
                "has_video": (entry / "video.mp4").exists(),
                "cost": (meta.get("cost") or {}).get("amount"),
                "error": meta.get("error"),
            }
        )

    jobs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return jobs


def job_path(settings: dict[str, Any], name: str) -> Path:
    """按目录名取任务目录，拒绝越权路径。"""
    root = output_root(settings).resolve()
    target = (root / name).resolve()
    if root not in target.parents and target != root:
        raise StoreError("非法的任务目录")
    if not target.is_dir():
        raise StoreError(f"任务目录不存在：{name}")
    return target
