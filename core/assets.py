"""资产库。

分两块，来源不同、寿命也不同：

**本地资产** —— `outputs/` 里的任务目录。视频、参数、提示词、参考素材副本
都在本地，**永久有效**。这是真正意义上的"资产"。

**云端任务** —— 调官方的 `GET /api/v1/tasks` 列出账号下的任务。
它能看到**任何工具**提交的任务（控制台、本工具、别的东西），
但有两条官方硬限制：
  - 查询时间跨度最大 24 小时
  - 已结束的任务超时后会被系统清理，过期就永久查不到

所以云端那块的作用是"**发现近期遗漏**"——把没存到本地的补回来。
长期保存靠本地。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import dashscope, logging_setup, pricing, probe, store
from .config import get_base_url

logger = logging_setup.get_logger()

THUMB_NAME = "thumb.jpg"
THUMB_WIDTH = 640
THUMB_QUALITY = 4          # ffmpeg -q:v，数字越小越清晰
THUMB_AT_SECONDS = 1.0     # 从第几秒抽帧（避开开头的黑场）
THUMB_MAX_AGE_DAYS = 365

CLOUD_WINDOW_HOURS = 24    # 官方限制：一次最多查 24 小时跨度


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


# ---------------------------------------------------------------- 缩略图

def ensure_thumbnail(job_dir: Path) -> Path | None:
    """给任务目录生成视频缩略图，缓存成 thumb.jpg。

    只在视频比缩略图新、或者还没有缩略图时才跑 ffmpeg，
    否则每次打开资产页都抽一遍帧太浪费。
    """
    video = job_dir / "video.mp4"
    thumb = job_dir / THUMB_NAME
    if not video.exists():
        return None

    if thumb.exists() and thumb.stat().st_mtime >= video.stat().st_mtime:
        return thumb

    if not _has_ffmpeg():
        return None

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-ss", str(THUMB_AT_SECONDS),
                "-i", str(video),
                "-frames:v", "1",
                "-vf", f"scale={THUMB_WIDTH}:-2",
                "-q:v", str(THUMB_QUALITY),
                str(thumb), "-y",
            ],
            capture_output=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("生成缩略图失败 | 目录=%s | %s", job_dir.name, exc)
        return None

    if result.returncode != 0 or not thumb.exists():
        # 视频可能短于 1 秒，退回从第 0 秒抽
        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(video), "-frames:v", "1",
                 "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", str(THUMB_QUALITY),
                 str(thumb), "-y"],
                capture_output=True, timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if not thumb.exists():
            return None

    return thumb


# ---------------------------------------------------------------- 本地资产

def _local_record(job_dir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    params = meta.get("parameters") or {}
    prompt = meta.get("prompt") or {}
    usage = meta.get("usage") or {}
    cost = meta.get("cost") or {}
    video = job_dir / "video.mp4"

    refs = _collect_refs(job_dir, meta)

    return {
        "source": "local",
        "dir": job_dir.name,
        "task_id": meta.get("task_id"),
        "status": meta.get("status"),
        "model": meta.get("model"),
        "created_at": meta.get("created_at"),
        "prompt": prompt.get("final") or prompt.get("original") or "",
        "original_prompt": prompt.get("original") or "",
        "optimized_prompt": prompt.get("optimized") or "",
        "resolution": params.get("resolution"),
        "ratio": params.get("ratio"),
        "duration": params.get("duration"),
        "duration_mode": params.get("duration_mode"),
        "audio": params.get("audio"),
        "watermark": params.get("watermark"),
        "prompt_extend": params.get("prompt_extend"),
        "seed": params.get("seed"),
        "mode": meta.get("mode"),
        "refs": refs,
        "has_video": video.exists(),
        "video_bytes": video.stat().st_size if video.exists() else 0,
        "actual_seconds": usage.get("output_video_duration"),
        "billable_seconds": usage.get("duration"),
        "cost": cost.get("amount"),
        "error": meta.get("error"),
        # 资产库自己的字段
        "favorite": bool(meta.get("favorite")),
        "tags": meta.get("tags") or [],
    }


VIDEO_SUFFIXES = {".mp4", ".mov"}
AUDIO_SUFFIXES = {".wav", ".mp3"}


def _collect_refs(job_dir: Path, meta: dict[str, Any]) -> list[dict[str, Any]]:
    """列出任务目录里的参考素材副本。

    复用参数时要靠这些把素材带回来，所以除了文件名，还要给出：
      - type：原来的素材类型（首帧/尾帧/参考图/参考视频/参考音频）
      - path：本地绝对路径，重新提交时直接拿它去上传
      - preview：界面预览地址

    meta.json 里记了每份素材的类型，按文件名对回去。对不上的（比如云端补下载的
    任务本来就没有素材）按扩展名猜一个。
    """
    ref_dir = job_dir / "refs"
    if not ref_dir.is_dir():
        return []

    recorded = {
        a.get("name"): a
        for a in (meta.get("assets") or [])
        if a.get("name")
    }

    refs: list[dict[str, Any]] = []
    for path in sorted(ref_dir.iterdir(), key=lambda p: p.name):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in VIDEO_SUFFIXES:
            fallback = "reference_video"
        elif suffix in AUDIO_SUFFIXES:
            fallback = "reference_audio"
        else:
            fallback = "reference_image"

        name = path.name
        saved = recorded.get(name) or {}
        refs.append({
            "name": name,
            "type": saved.get("type") or fallback,
            "path": str(path),
            "preview": f"/api/assets/{job_dir.name}/refs/{name}",
            "duration": saved.get("duration") or 0,
        })
    return refs


def refs_with_duration(job_dir: Path, meta: dict[str, Any]) -> list[dict[str, Any]]:
    """带时长的参考素材。

    老任务的 meta.json 里没记时长，但成本预估要用（参考视频的秒数也计费），
    所以这里现场探一次。只在用户点「复用参数」时调，不会拖慢列表页。
    """
    refs = _collect_refs(job_dir, meta)
    for ref in refs:
        if ref["type"] == "reference_video" and not ref.get("duration"):
            info = probe.inspect(Path(ref["path"]))
            ref["duration"] = info.get("duration") or 0
    return refs


def local_assets(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """扫 outputs/，返回全部本地资产（新的在前）。"""
    try:
        root = store.output_root(settings)
    except Exception:  # noqa: BLE001
        return []

    assets: list[dict[str, Any]] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        meta = store.read_meta(entry)
        if meta is None:
            continue
        assets.append(_local_record(entry, meta))

    assets.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    return assets


def set_favorite(job_dir: Path, value: bool) -> None:
    meta = store.read_meta(job_dir) or {}
    meta["favorite"] = bool(value)
    store.write_meta(job_dir, meta)


def set_tags(job_dir: Path, tags: list[str]) -> list[str]:
    cleaned = [t.strip() for t in tags if t and t.strip()][:20]
    meta = store.read_meta(job_dir) or {}
    meta["tags"] = cleaned
    store.write_meta(job_dir, meta)
    return cleaned


def delete_asset(job_dir: Path) -> None:
    """删掉整个任务目录。目录名由 store 校验过，不会跑出去。"""
    if not job_dir.is_dir():
        return
    shutil.rmtree(job_dir)


# ---------------------------------------------------------------- 云端任务

def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S")


def cloud_tasks(
    settings: dict[str, Any],
    *,
    hours: int = 24,
    model_name: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """列出账号下的异步任务。

    官方限制一次最多查 24 小时跨度，所以要查更久就按 24 小时窗口往回翻。
    hours 上限 168（7 天）—— 再久的基本已经被系统清理了，查了也是空。
    """
    api_key = (settings.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("还没配置 API Key，请先在「设置」里填写。")

    base_url = get_base_url(settings)
    hours = max(1, min(int(hours), 168))

    collected: list[dict[str, Any]] = []
    now = datetime.now()
    remaining = hours
    seen_ids: set[str] = set()

    while remaining > 0:
        span = min(remaining, CLOUD_WINDOW_HOURS)
        end = now
        start = now - timedelta(hours=span)
        page = 1

        while True:
            payload = dashscope.list_tasks(
                base_url, api_key,
                start_time=_fmt_time(start), end_time=_fmt_time(end),
                model_name=model_name, status=status,
                page_no=page, page_size=50,
            )
            rows = payload.get("data") or []
            for row in rows:
                task_id = row.get("task_id")
                if task_id and task_id not in seen_ids:
                    seen_ids.add(task_id)
                    collected.append(row)

            total_page = int(payload.get("total_page") or 1)
            if page >= total_page or page >= 20:   # 每窗口最多翻 20 页，防跑飞
                break
            page += 1

        now = start
        remaining -= span

    collected.sort(key=lambda r: r.get("gmt_create") or 0, reverse=True)

    return {
        "tasks": collected,
        "hours": hours,
        "note": (
            "官方限制：单个查询窗口最大 24 小时，且已结束的任务超时后会被系统清理。"
            "查不到不代表没生成过，只代表已经过期。"
        ),
    }


def merged_view(settings: dict[str, Any], *, hours: int = 24) -> dict[str, Any]:
    """本地资产 + 云端任务，合成一个视图。

    云端里本地已经有的，标记出来；本地没有的，标成"可补下载"。
    """
    assets = local_assets(settings)
    local_task_ids = {a["task_id"] for a in assets if a.get("task_id")}

    try:
        cloud = cloud_tasks(settings, hours=hours)
        tasks = cloud["tasks"]
        cloud_error = None
        note = cloud["note"]
    except Exception as exc:  # noqa: BLE001 - 云端查不到不该让整页打不开
        tasks = []
        cloud_error = str(exc)
        note = ""

    cloud_records = []
    for task in tasks:
        task_id = task.get("task_id")
        cloud_records.append({
            "source": "cloud",
            "task_id": task_id,
            "model": task.get("model_name"),
            "status": task.get("status"),
            "created_at": _ms_to_iso(task.get("gmt_create")),
            "started_at": _ms_to_iso(task.get("start_time")),
            "ended_at": _ms_to_iso(task.get("end_time")),
            "region": task.get("region"),
            "in_local": task_id in local_task_ids,
        })

    return {
        "assets": assets,
        "cloud": cloud_records,
        "cloud_error": cloud_error,
        "note": note,
        "counts": {
            "local": len(assets),
            "cloud": len(cloud_records),
            "cloud_missing": sum(1 for c in cloud_records if not c["in_local"]),
            "favorite": sum(1 for a in assets if a.get("favorite")),
        },
    }


def stats(settings: dict[str, Any], *, max_days: int = 30) -> dict[str, Any]:
    """仪表盘用的汇总。全部来自本地 meta.json，不调任何接口。"""
    records = local_assets(settings)

    total_cost = 0.0
    total_seconds = 0.0
    ok = 0
    for asset in records:
        total_cost += float(asset.get("cost") or 0)
        total_seconds += float(asset.get("billable_seconds") or 0)
        if asset.get("status") == "succeeded":
            ok += 1

    count = len(records)
    totals = {
        "count": count,
        "cost": round(total_cost, 2),
        "seconds": round(total_seconds, 1),
        "succeeded": ok,
        "failed": count - ok,
        "success_rate": round(ok / count * 100, 1) if count else None,
        # 平均每条多少钱 —— 用户最该盯的数字之一
        "avg_cost": round(total_cost / count, 2) if count else None,
        "avg_seconds": round(total_seconds / count, 1) if count else None,
        "favorite": sum(1 for a in records if a.get("favorite")),
    }

    # 按天汇总。中间没有生成的日子也要占位，否则时间轴会被压缩、看不出间隔。
    daily_map: dict[str, dict[str, Any]] = {}
    for asset in records:
        day = (asset.get("created_at") or "")[:10]
        if not day:
            continue
        bucket = daily_map.setdefault(day, {"date": day, "cost": 0.0, "count": 0})
        bucket["cost"] += float(asset.get("cost") or 0)
        bucket["count"] += 1

    daily: list[dict[str, Any]] = []
    if daily_map:
        first = datetime.fromisoformat(min(daily_map))
        last = datetime.fromisoformat(max(daily_map))
        span = (last - first).days
        # 跨度太大就只画最近的一段，否则一根柱子会细到看不见
        if span > max_days:
            first = last - timedelta(days=max_days)
        cursor = first
        while cursor <= last:
            key = cursor.strftime("%Y-%m-%d")
            bucket = daily_map.get(key)
            daily.append({
                "date": key,
                "cost": round(bucket["cost"], 2) if bucket else 0.0,
                "count": bucket["count"] if bucket else 0,
            })
            cursor += timedelta(days=1)

    # 按分辨率
    by_res: dict[str, dict[str, Any]] = {}
    for asset in records:
        key = asset.get("resolution") or "未知"
        bucket = by_res.setdefault(key, {
            "resolution": key, "cost": 0.0, "count": 0, "seconds": 0.0,
        })
        bucket["cost"] += float(asset.get("cost") or 0)
        bucket["seconds"] += float(asset.get("billable_seconds") or 0)
        bucket["count"] += 1
    by_resolution = sorted(
        by_res.values(), key=lambda r: r["cost"], reverse=True,
    )
    for bucket in by_resolution:
        bucket["cost"] = round(bucket["cost"], 2)
        bucket["seconds"] = round(bucket["seconds"], 1)

    ranked = sorted(records, key=lambda a: float(a.get("cost") or 0), reverse=True)

    return {
        "totals": totals,
        "daily": daily,
        "by_resolution": by_resolution,
        "top_costs": [
            {
                "dir": a["dir"], "cost": a.get("cost"), "resolution": a.get("resolution"),
                "billable_seconds": a.get("billable_seconds"),
                "prompt": (a.get("prompt") or "")[:60],
            }
            for a in ranked[:5] if (a.get("cost") or 0) > 0
        ],
        "recent": [
            {
                "dir": a["dir"], "created_at": a.get("created_at"),
                "resolution": a.get("resolution"), "duration": a.get("duration"),
                "billable_seconds": a.get("billable_seconds"), "cost": a.get("cost"),
                "status": a.get("status"), "prompt": (a.get("prompt") or "")[:60],
            }
            for a in records[:10]
        ],
    }


def _ms_to_iso(value: Any) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return None


# ---------------------------------------------------------------- 补下载

def adopt_task(settings: dict[str, Any], task_id: str) -> dict[str, Any]:
    """把云端的一条任务补下载到本地资产库。

    用于：视频是用控制台或别的工具生成的，本地没有副本。
    只要还在 24 小时链接有效期内，就能取回来。
    """
    api_key = (settings.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("还没配置 API Key。")
    base_url = get_base_url(settings)

    result = dashscope.query_task(base_url, api_key, task_id)
    output = result.get("output") or {}
    usage = result.get("usage") or {}

    if result.get("task_status") != "SUCCEEDED":
        raise RuntimeError(
            f"这个任务不是成功状态（{result.get('task_status')}），无法下载。"
            f"{output.get('message') or ''}"
        )

    video_url = output.get("video_url")
    if not video_url:
        raise RuntimeError(
            "任务成功了，但官方没返回视频链接——多半是已超过 24 小时有效期，链接被清掉了。"
        )

    prompt = output.get("orig_prompt") or ""
    model = settings.get("model") or "wan3.0-video"
    job_dir = store.new_job_dir(settings, prompt or f"云端任务_{task_id[:8]}")

    size = dashscope.download_video(
        video_url, job_dir / "video.mp4",
        retries=int(settings.get("download_retries", 3)),
    )

    meta = {
        "job_id": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "base_url": base_url,
        "region": settings.get("region"),
        "task_id": task_id,
        "task_status": "SUCCEEDED",
        "adopted": True,
        "adopted_note": "这条不是本工具提交的，是通过官方任务列表接口发现后补下载的。",
        "prompt": {"original": prompt, "optimized": "", "final": prompt,
                   "optimize_enabled": False, "prompt_extend": None},
        "parameters": {
            "resolution": _resolution_from_usage(usage),
            "ratio": usage.get("ratio"),
            "duration": usage.get("output_video_duration"),
            "duration_mode": "custom",
            "audio": None,
            "seed": None,
            "prompt_extend": None,
            "watermark": None,
        },
        "assets": [],
        "mode": "云端补下载",
        "status": "succeeded",
        "usage": usage,
        "video": {
            "file": "video.mp4", "size_bytes": size, "source_url": video_url,
            "note": "补下载。原始参数官方不保留，只有时长/分辨率/比例能从用量里推出来。",
        },
        "cost": pricing.actual_cost(settings, model, usage),
        "request_ids": {"补下载查询": result.get("request_id")},
    }
    store.write_meta(job_dir, meta)

    logger.info("补下载云端任务 | task_id=%s | 目录=%s | %s bytes", task_id, job_dir.name, size)

    return {
        "dir": job_dir.name,
        "task_id": task_id,
        "size_bytes": size,
        "prompt": prompt,
    }


def _resolution_from_usage(usage: dict[str, Any]) -> str | None:
    sr = usage.get("SR")
    try:
        return {480: "480P", 720: "720P", 1080: "1080P"}.get(int(sr))
    except (TypeError, ValueError):
        return None
