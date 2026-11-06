"""万相3.0 视频创作台 —— 本地服务。

启动：python app.py   然后浏览器打开 http://127.0.0.1:8000

刻意只监听 127.0.0.1：API Key 存在本机 settings.json 里，不对外暴露。
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from core import (
    assets,
    config,
    dashscope,
    image_fix,
    jobs,
    logging_setup,
    pricing,
    probe,
    prompt_opt,
    rules,
    store,
    upscale,
    video_analysis,
    version,
    voice,
    voice_service,
    watermark,
)

# 每次进程启动换一个，前端用它判断服务端有没有重启过
SERVER_INSTANCE = uuid.uuid4().hex[:12]

logger = logging_setup.get_logger()

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
STAGING_DIR = ROOT / "_staging"
STAGING_TTL_SECONDS = 7 * 24 * 3600

STAGING_DIR.mkdir(exist_ok=True)

app = FastAPI(title="万相3.0 视频创作台")
manager = jobs.JobManager()


# ---------------------------------------------------------------- 版本

@app.middleware("http")
async def no_cache_for_static(request, call_next):
    """让浏览器每次都回来问一下静态资源有没有变。

    不加这个头的话，浏览器会按启发式规则自己缓存，改完代码用户刷新也拿不到新版
    —— 而且版本监控本身就在被缓存的 JS 里，形成死循环：旧 JS 检测不到新版本，
    于是永远不提示刷新。

    用 no-cache（每次校验）而不是 no-store（完全不存）：服务端有 ETag，
    文件没变就是 304，几十字节，很快。
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/api/version")
def get_version() -> dict[str, Any]:
    """给前端的版本戳。

    页面加载时记一份，之后定期比对：
      - web 变了  -> 浏览器里跑的是旧 JS，必须刷新，否则行为和服务端对不上
      - instance 变了 -> 服务端重启过（任务会被自动接回，但界面该刷新一下）
    """
    return {
        "web": version.web_stamp(),
        "code": version.code_stamp(),
        "instance": SERVER_INSTANCE,
    }


# ---------------------------------------------------------------- 设置

@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return config.public_settings()


class SettingsPayload(BaseModel):
    # api_key 为空表示「不修改已存的 Key」，避免前端每次保存都把 Key 抹掉
    api_key: str | None = None
    region: str | None = None
    endpoint_style: str | None = None
    workspace_id: str | None = None
    model: str | None = None
    output_dir: str | None = None
    poll_interval_seconds: int | None = None
    download_retries: int | None = None
    keep_reference_copies: bool | None = None
    prompt_helper: dict[str, Any] | None = None
    pricing: dict[str, Any] | None = None


@app.post("/api/settings")
def save_settings(payload: SettingsPayload) -> dict[str, Any]:
    current = config.load_settings()
    updates = payload.model_dump(exclude_none=True)

    if not updates.get("api_key"):
        updates.pop("api_key", None)

    if "region" in updates and updates["region"] not in config.REGIONS:
        raise HTTPException(400, f"未知地域：{updates['region']}")

    current.update(updates)

    try:
        config.get_base_url(current)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    config.save_settings(current)
    return {"ok": True, "settings": config.public_settings()["settings"]}


@app.post("/api/settings/test")
def test_settings() -> dict[str, Any]:
    settings = config.load_settings()
    api_key = (settings.get("api_key") or "").strip()
    if not api_key:
        return {"ok": False, "message": "还没填写 API Key。"}
    try:
        base_url = config.get_base_url(settings)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}

    result = dashscope.test_connection(base_url, api_key)
    result["base_url"] = base_url
    return result


# ---------------------------------------------------------------- 素材暂存与预检

@app.post("/api/staging")
async def stage_files(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    """接收浏览器选中的本地文件，存到本机暂存区并立刻做规格预检。

    文件先落在本地，是为了：(1) 提交前就能预检格式；(2) 落盘时能复制进任务目录。
    """
    staged: list[dict[str, Any]] = []

    for upload in files:
        original_name = Path(upload.filename or "file").name
        token = uuid.uuid4().hex[:12]
        folder = STAGING_DIR / token
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / original_name

        try:
            with target.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    handle.write(chunk)
        except OSError as exc:
            raise HTTPException(500, f"保存文件失败：{exc}")

        info = probe.inspect(target)

        # 官方不支持带透明通道的图像，而 Windows 截图默认就是 RGBA PNG。
        # 与其让用户每次自己转格式，不如在这里直接压平到白底。
        fix: dict[str, Any] = {"applied": False}
        if info.get("kind") == "image":
            target, fix = image_fix.ensure_no_alpha(target)
            if fix.get("applied"):
                target = Path(fix["output"])
                # 修完重新探测，让规格校验基于实际要上传的那份文件
                info = probe.inspect(target)

        info["staging_id"] = token
        info["staged_path"] = str(target)
        info["preview_url"] = f"/api/staging/{token}/{target.name}"
        info["auto_fix"] = fix
        staged.append(info)

    return {
        "files": staged,
        "ffprobe": probe.ffprobe_available(),
        "image_fix_available": image_fix.PILLOW_AVAILABLE,
    }


@app.post("/api/probe/validate")
def validate_media(items: list[dict[str, Any]]) -> dict[str, Any]:
    """跨素材的约束（合计时长、数量上限）需要整批一起判断。"""
    prepared: list[dict[str, Any]] = []
    for item in items:
        path = Path(item.get("staged_path", ""))
        result = probe.inspect(path) if path.exists() else {
            "name": item.get("name", ""),
            "kind": "unknown",
            "errors": ["暂存文件已丢失，请重新选择"],
            "warnings": [],
        }
        result["media_type"] = item.get("media_type")
        prepared.append(result)
    return {"files": probe.validate_batch(prepared)}


@app.get("/api/staging/{token}/{name}")
def get_staged(token: str, name: str) -> FileResponse:
    target = _safe_child(STAGING_DIR / token, name)
    return FileResponse(target)


def _safe_child(parent: Path, name: str) -> Path:
    """拼路径并确认没跑出目录，挡掉 ../ 之类的越权访问。"""
    base = parent.resolve()
    target = (base / name).resolve()
    if base != target.parent and base not in target.parents:
        raise HTTPException(400, "非法路径")
    if not target.exists():
        raise HTTPException(404, "文件不存在")
    return target


# ---------------------------------------------------------------- 提示词整理

class OptimizePayload(BaseModel):
    prompt: str
    assets: list[dict[str, Any]] = []


@app.post("/api/prompt/optimize")
def optimize_prompt(payload: OptimizePayload) -> dict[str, Any]:
    """按官方 wan3-pe Skill 整理提示词。

    这一层是可选的前置步骤，服务端的 prompt_extend 是独立的一层，两者不冲突。
    """
    settings = config.load_settings()
    api_key = (settings.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "还没配置 API Key，请先在「设置」里填写。")

    if not prompt_opt.skill_available():
        raise HTTPException(
            500, "找不到官方 Skill 文件 skills/wan3-pe/SKILL.md，无法整理提示词。"
        )

    try:
        base_url = config.get_compatible_base_url(settings)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    try:
        return prompt_opt.optimize(
            settings,
            base_url=base_url,
            api_key=api_key,
            user_prompt=payload.prompt,
            assets=payload.assets,
        )
    except prompt_opt.PromptHelperError as exc:
        raise HTTPException(400, str(exc))


class TestPromptModelPayload(BaseModel):
    model: str
    slot: Literal["文本模型", "视觉模型"] = "文本模型"


@app.post("/api/prompt/test")
def test_prompt_model(payload: TestPromptModelPayload) -> dict[str, Any]:
    """测某个文本模型能不能用。403 access_denied 只能这样单独试出来。"""
    settings = config.load_settings()
    api_key = (settings.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "还没配置 API Key，请先在「设置」里填写。")

    model = (payload.model or "").strip()
    if not model:
        raise HTTPException(400, "请先填写模型名。")

    try:
        base_url = config.get_compatible_base_url(settings)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return prompt_opt.test_model(
        settings,
        base_url=base_url,
        api_key=api_key,
        model=model,
        slot=payload.slot,
    )


# ---------------------------------------------------------------- 视频分析

class VideoAnalysisPayload(BaseModel):
    baseline: video_analysis.VideoBaseline
    include_audio_metrics: bool = False


def _run_video_analysis(
    path: Path,
    video_name: str,
    baseline: video_analysis.VideoBaseline,
    include_audio_metrics: bool = False,
) -> dict[str, Any]:
    settings = config.load_settings()
    api_key = (settings.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "还没配置 API Key，请先在「设置」里填写。")

    try:
        base_url = config.get_compatible_base_url(settings)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    helper = settings.get("prompt_helper") or {}
    model = (helper.get("vision_model") or "").strip()
    try:
        return video_analysis.analyze(
            path,
            video_name=video_name,
            baseline=baseline,
            model=model,
            base_url=base_url,
            api_key=api_key,
            include_audio_metrics=include_audio_metrics,
        )
    except prompt_opt.PromptHelperError as exc:
        raise HTTPException(502, str(exc))
    except video_analysis.VideoAnalysisError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/video-analysis")
def analyze_uploaded_video(
    file: UploadFile = File(...),
    baseline: str = Form(...),
    include_audio_metrics: bool = Form(False),
) -> dict[str, Any]:
    try:
        baseline_data = video_analysis.VideoBaseline.model_validate_json(baseline)
    except ValidationError as exc:
        raise HTTPException(422, f"账号基线填写有误：{exc.errors(include_input=False)[:6]}")

    original_name = Path(file.filename or "video").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in {".mp4", ".mov"}:
        raise HTTPException(400, "只支持 MP4 或 MOV 格式的视频。")

    with tempfile.TemporaryDirectory(prefix="wan3-analysis-upload-") as temp:
        target = Path(temp) / f"video{suffix}"
        total = 0
        try:
            with target.open("wb") as output:
                while chunk := file.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > video_analysis.MAX_VIDEO_BYTES:
                        raise HTTPException(413, "视频超过 500MB 分析上限。")
                    output.write(chunk)
        except OSError as exc:
            raise HTTPException(500, f"保存待分析视频失败：{exc}")
        finally:
            file.file.close()
        return _run_video_analysis(
            target, original_name, baseline_data, include_audio_metrics
        )


@app.post("/api/video-analysis/assets/{name}")
def analyze_saved_video(name: str, payload: VideoAnalysisPayload) -> dict[str, Any]:
    settings = config.load_settings()
    job_dir = _asset_dir(settings, name)
    target = job_dir / "video.mp4"
    if not target.is_file():
        raise HTTPException(404, "这个资产没有已保存的视频文件。")
    return _run_video_analysis(
        target, name, payload.baseline, payload.include_audio_metrics
    )


# ---------------------------------------------------------------- 成本预估

class EstimatePayload(BaseModel):
    model: str
    resolution: str
    duration: int
    input_video_seconds: float = 0.0


@app.post("/api/estimate")
def estimate(payload: EstimatePayload) -> dict[str, Any]:
    settings = config.load_settings()
    result = pricing.estimate(
        settings,
        payload.model,
        payload.resolution,
        payload.duration,
        payload.input_video_seconds,
    )
    result["breakdown"] = pricing.price_breakdown(
        settings, payload.model, payload.resolution
    )
    result["free_quota_seconds"] = (
        settings.get("pricing", {}).get("free_quota_seconds")
    )
    return result


# ---------------------------------------------------------------- 生成

class GeneratePayload(BaseModel):
    prompt: str
    original_prompt: str = ""
    optimized_prompt: str = ""
    optimize_enabled: bool = False
    model: str
    resolution: str
    ratio: str
    duration: int
    audio: bool = True
    seed: Any = -1
    prompt_extend: bool = True
    watermark: bool = False
    assets: list[dict[str, Any]] = []


@app.post("/api/generate")
def generate(payload: GeneratePayload) -> dict[str, Any]:
    settings = config.load_settings()
    if not (settings.get("api_key") or "").strip():
        raise HTTPException(400, "还没配置 API Key，请先在「设置」里填写。")

    data = payload.model_dump()
    if not data.get("prompt", "").strip() and not data.get("assets"):
        raise HTTPException(400, "提示词和参考素材至少要有一个。")

    media_types = [a.get("media_type", "") for a in data["assets"]]
    combo_errors = rules.validate_combo(media_types)
    if combo_errors:
        raise HTTPException(400, "；".join(combo_errors))

    locked, _ = rules.prompt_extend_locked(media_types)
    if locked:
        # 官方硬性要求，静默纠正而不是报错
        data["prompt_extend"] = True

    job = manager.submit(data)
    return {"job_id": job.id, "dir": job.job_dir.name}


@app.get("/api/jobs")
def list_active_jobs() -> dict[str, Any]:
    """当前还在跑的任务。

    界面靠它发现「服务重启后自动接回来的任务」—— 那些任务的 job_id 是新的，
    前端并不知道，只能通过列表发现。
    """
    return {"jobs": [job.to_dict() for job in manager.active_jobs()]}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在（服务重启后历史任务请走「历史」页）")
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    if not manager.cancel(job_id):
        raise HTTPException(400, "任务不存在或已结束")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/video")
def job_video(job_id: str) -> FileResponse:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    target = job.job_dir / "video.mp4"
    if not target.exists():
        raise HTTPException(404, "视频还没生成好")
    return FileResponse(target, media_type="video/mp4", filename="video.mp4")


# ---------------------------------------------------------------- 资产库

@app.get("/api/assets")
def get_assets(hours: int = 24) -> dict[str, Any]:
    """本地资产 + 云端任务，合成一个视图。

    云端那块查不到（没配 Key、网络问题）也不该让整页打不开，
    所以 merged_view 内部会把错误收进 cloud_error。
    """
    settings = config.load_settings()
    try:
        return assets.merged_view(settings, hours=max(1, min(int(hours), 168)))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("读取资产库失败：%s", exc)
        raise HTTPException(500, f"读取资产库失败：{exc}")


def _asset_dir(settings: dict[str, Any], name: str) -> Path:
    try:
        return store.job_path(settings, name)
    except store.StoreError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/assets/{name}/thumb")
def asset_thumb(name: str) -> FileResponse:
    """视频缩略图。第一次访问时用 ffmpeg 抽帧，之后走缓存。"""
    settings = config.load_settings()
    job_dir = _asset_dir(settings, name)
    thumb = assets.ensure_thumbnail(job_dir)
    if thumb is None:
        raise HTTPException(404, "生成缩略图失败（视频不存在，或本机没有 ffmpeg）")
    return FileResponse(thumb, media_type="image/jpeg")


@app.get("/api/assets/{name}/video")
def asset_video(name: str) -> FileResponse:
    settings = config.load_settings()
    target = _asset_dir(settings, name) / "video.mp4"
    if not target.exists():
        raise HTTPException(404, "这个任务没有视频文件")
    return FileResponse(target, media_type="video/mp4", filename="video.mp4")


@app.get("/api/assets/{name}/refs/{filename}")
def asset_ref(name: str, filename: str) -> FileResponse:
    settings = config.load_settings()
    target = _safe_child(_asset_dir(settings, name) / "refs", filename)
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type)


class FavoritePayload(BaseModel):
    value: bool


@app.post("/api/assets/{name}/favorite")
def asset_favorite(name: str, payload: FavoritePayload) -> dict[str, Any]:
    settings = config.load_settings()
    assets.set_favorite(_asset_dir(settings, name), payload.value)
    return {"ok": True, "favorite": payload.value}


class TagsPayload(BaseModel):
    tags: list[str] = []


@app.post("/api/assets/{name}/tags")
def asset_tags(name: str, payload: TagsPayload) -> dict[str, Any]:
    settings = config.load_settings()
    saved = assets.set_tags(_asset_dir(settings, name), payload.tags)
    return {"ok": True, "tags": saved}


@app.delete("/api/assets/{name}")
def asset_delete(name: str) -> dict[str, Any]:
    settings = config.load_settings()
    job_dir = _asset_dir(settings, name)
    logger.info("删除资产 | 目录=%s", name)
    assets.delete_asset(job_dir)
    return {"ok": True, "deleted": name}


class AdoptPayload(BaseModel):
    task_id: str


@app.post("/api/assets/adopt")
def asset_adopt(payload: AdoptPayload) -> dict[str, Any]:
    """把云端发现的任务补下载到本地资产库。"""
    settings = config.load_settings()
    try:
        return assets.adopt_task(settings, payload.task_id)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("补下载失败 | task_id=%s | %s", payload.task_id, exc)
        raise HTTPException(500, f"补下载失败：{exc}")


# ---------------------------------------------------------------- 历史

@app.get("/api/history")
def history() -> dict[str, Any]:
    settings = config.load_settings()
    return {"jobs": store.list_jobs(settings)}


# ---------------------------------------------------------------- 视频超分

UPSCALE_DIR = ROOT / "_upscale"
UPSCALE_DIR.mkdir(exist_ok=True)

# 任务记录落盘到 _upscale/jobs/，服务重启后还能查、还能下载
upscale_manager = upscale.UpscaleManager(UPSCALE_DIR / "jobs")


def suffix_for(target: str) -> str:
    """成品文件名里的那一截。native 就是「分辨率不变、只提画质」。"""
    return "画质增强" if target == "native" else target


class UpscalePayload(BaseModel):
    # asset = 资产库里的任务目录名；staged = 暂存区里的本地文件绝对路径
    source_type: str = "asset"
    source: str
    # native = 分辨率不变，只提升画质（默认）
    target: str = "native"
    # 实测 ×2 的回缩效果比 ×3 更好，成本只有 1/2.25
    scale: int = 2
    model: str = "realesr-animevideov3"
    chunk: int = 120
    max_temp: int = 75
    max_util: int = 90


@app.post("/api/upscale")
def start_upscale(payload: UpscalePayload) -> dict[str, Any]:
    if payload.target not in ("1080p", "2k", "4k", "native"):
        raise HTTPException(400, f"不支持的目标分辨率：{payload.target}")
    if payload.scale not in (2, 3, 4):
        raise HTTPException(400, "放大倍数只能是 2 / 3 / 4")
    if payload.model not in upscale.upscale_video.MODELS:
        raise HTTPException(400, f"不认识的模型：{payload.model}")
    if not (10 <= payload.chunk <= 2000):
        raise HTTPException(400, "每块帧数要在 10~2000 之间")
    for name, value in (("温度上限", payload.max_temp), ("占用上限", payload.max_util)):
        if value != 0 and not (20 <= value <= 100):
            raise HTTPException(400, f"{name}要么填 0（不限），要么在 20~100 之间")

    settings = config.load_settings()

    if payload.source_type == "asset":
        try:
            job_dir = store.job_path(settings, payload.source)
        except store.StoreError as exc:
            raise HTTPException(404, str(exc))
        source = job_dir / "video.mp4"
        if not source.exists():
            raise HTTPException(404, "这条资产没有视频文件")
        output = job_dir / f"video_{suffix_for(payload.target)}.mp4"
        label = payload.source

    elif payload.source_type == "staged":
        source = Path(payload.source)
        # 只允许读暂存区里的文件，别让接口变成任意文件读取
        staging_root = (ROOT / "_staging").resolve()
        try:
            resolved = source.resolve()
        except OSError:
            raise HTTPException(400, "路径无效")
        if staging_root not in resolved.parents:
            raise HTTPException(400, "只能处理暂存区里的文件，请先上传")
        if not resolved.exists():
            raise HTTPException(404, "暂存文件已失效，请重新上传")
        source = resolved
        output = UPSCALE_DIR / f"{source.stem}_{suffix_for(payload.target)}.mp4"
        label = source.name
    else:
        raise HTTPException(400, "source_type 只能是 asset 或 staged")

    job = upscale_manager.submit(
        source, output,
        target=payload.target,
        scale=payload.scale,
        model=payload.model,
        label=label,
        chunk=payload.chunk,
        max_temp=payload.max_temp,
        max_util=payload.max_util,
    )
    logger.info("开始超分 | %s -> %s | 模型=%s ×%d",
                source.name, payload.target, payload.model, payload.scale)
    return job.to_dict()


@app.get("/api/upscale/jobs")
def upscale_jobs() -> dict[str, Any]:
    return {"jobs": [job.to_dict() for job in upscale_manager.active()]}


@app.get("/api/upscale/history")
def upscale_history() -> dict[str, Any]:
    """所有超分任务，最新的在前。服务重启后依然可查。"""
    return {"jobs": [job.to_dict() for job in upscale_manager.history()]}


@app.get("/api/upscale/{job_id}")
def upscale_status(job_id: str) -> dict[str, Any]:
    job = upscale_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job.to_dict()


@app.delete("/api/upscale/{job_id}")
def upscale_forget(job_id: str) -> dict[str, Any]:
    """只删任务记录，不动成品文件。"""
    if not upscale_manager.forget(job_id):
        raise HTTPException(404, "任务不存在")
    return {"ok": True}


@app.get("/api/upscale/{job_id}/video")
def upscale_video(job_id: str) -> FileResponse:
    """只要成品文件还在磁盘上就给，不管任务当时是什么状态。

    为什么不再卡 status == done：服务重启会把内存里的任务标成「已中断」，
    但那个文件可能是完整的。卡状态会让用户明明看到文件却下载不了。
    """
    job = upscale_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    target = Path(job.output)
    if not target.exists():
        raise HTTPException(404, "结果文件不见了，可能被清理了")
    return FileResponse(target, media_type="video/mp4", filename=target.name)


@app.get("/api/upscale/{job_id}/comparison")
def upscale_comparison(job_id: str) -> FileResponse:
    job = upscale_manager.get(job_id)
    if job is None or not job.comparison:
        raise HTTPException(404, "没有对比图")
    target = Path(job.comparison)
    if not target.exists():
        raise HTTPException(404, "对比图不见了")
    return FileResponse(target, media_type="image/jpeg", filename=target.name)


# ---------------------------------------------------------------- AI 去水印

WATERMARK_DIR = ROOT / "_watermark"
WATERMARK_DIR.mkdir(exist_ok=True)
# 任务记录落盘，服务重启后历史还能查、成品还能下载
watermark_manager = watermark.WatermarkManager(WATERMARK_DIR / "jobs")

WATERMARK_PRESETS = {
    "auto", "doubao", "liblib", "libtv", "wanxiang", "wanx",
    "jimeng", "kling", "hailuo",
    "generic-br", "generic-bl", "generic-tr", "generic-tl",
    "generic-top", "generic-bottom",
}


WATERMARK_MODES = {
    "sttn-auto", "sttn-det", "lama", "lama-direct", "propainter", "opencv",
}
WATERMARK_ADVANCED_RANGES = {
    "sttnNeighborStride": (1, 100),
    "sttnReferenceLength": (1, 100),
    "sttnMaxLoadNum": (1, 300),
    "propainterMaxLoadNum": (1, 300),
    "subtitleAreaDeviationPixel": (1, 300),
    "subtitleYXAxisDifferencePixel": (0, 300),
    "subtitleAreaYAxisDifferencePixel": (0, 300),
    "subtitleAreaPixelToleranceXPixel": (0, 300),
    "subtitleAreaPixelToleranceYPixel": (0, 300),
    "subtitleTimelineBackwardFrameCount": (0, 300),
    "subtitleTimelineForwardFrameCount": (0, 300),
}


class WatermarkPayload(BaseModel):
    # asset = 资产库里的任务目录名；staged = 暂存区里的本地文件绝对路径
    source_type: str = "staged"
    source: str
    mode: str = ""
    # 兼容旧前端：sttn / lama
    engine: str = ""
    detect_model: str = "fast"
    preset: str = "auto"
    areas: list[list[float]] | None = None
    box: list[float] | None = None
    ab: list[float] | None = None
    auto_detect: bool = False
    hardware_acceleration: bool = True
    advanced: dict[str, Any] | None = None
    detect_only: bool = False


def _clean_area(values: list[float], what: str = "选区") -> list[float]:
    if len(values) != 4:
        raise HTTPException(400, f"{what}需要 4 个数字：x1 y1 x2 y2")
    try:
        box = [float(v) for v in values]
    except (TypeError, ValueError):
        raise HTTPException(400, f"{what}必须是数字")
    if any(v < 0 or v > 1 for v in box):
        raise HTTPException(400, f"{what}是 0~1 的相对坐标")
    if box[0] >= box[2] or box[1] >= box[3]:
        raise HTTPException(400, f"{what}要满足 x1 < x2 且 y1 < y2")
    return box


@app.post("/api/watermark")
def start_watermark(payload: WatermarkPayload) -> dict[str, Any]:
    mode = (payload.mode or "").lower().strip()
    if not mode:
        legacy = (payload.engine or "sttn").lower()
        mode = "lama" if legacy == "lama" else "sttn-auto"
    if mode not in WATERMARK_MODES:
        raise HTTPException(400, f"不认识的算法：{mode}")

    detect_model = (payload.detect_model or "fast").lower()
    if detect_model not in ("fast", "precise"):
        raise HTTPException(400, "检测模型只能是 fast 或 precise")

    preset = (payload.preset or "auto").lower()
    if preset not in WATERMARK_PRESETS and preset != "manual":
        raise HTTPException(400, f"不认识的水印预设：{preset}")

    areas: list[list[float]] = []
    if payload.areas and not payload.auto_detect:
        if len(payload.areas) > 20:
            raise HTTPException(400, "选区最多 20 个")
        areas = [_clean_area(box) for box in payload.areas]
    if payload.box:
        areas = [_clean_area(payload.box)]
    if not payload.auto_detect and not areas:
        if mode in ("sttn-auto", "lama-direct"):
            raise HTTPException(400, "请先在画面上框出水印区域，或勾选自动检测")
        if payload.detect_only:
            raise HTTPException(400, "自动检测需要先勾选自动检测，或给出选区")

    ab = None
    if payload.ab:
        if len(payload.ab) != 2:
            raise HTTPException(400, "A/B 区间需要两个秒数")
        try:
            ab = [float(payload.ab[0]), float(payload.ab[1])]
        except (TypeError, ValueError):
            raise HTTPException(400, "A/B 区间必须是数字")
        if ab[0] < 0 or ab[1] <= ab[0]:
            raise HTTPException(400, "A/B 区间要满足 0 ≤ A < B")

    advanced: dict[str, int] = {}
    for key, value in (payload.advanced or {}).items():
        if key not in WATERMARK_ADVANCED_RANGES:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        low, high = WATERMARK_ADVANCED_RANGES[key]
        advanced[key] = max(low, min(high, number))

    settings = config.load_settings()
    if payload.source_type == "asset":
        try:
            job_dir = store.job_path(settings, payload.source)
        except store.StoreError as exc:
            raise HTTPException(404, str(exc))
        source = job_dir / "video.mp4"
        if not source.exists():
            raise HTTPException(404, "这条资产没有视频文件")
        meta = store.read_meta(job_dir) or {}
        label = (meta.get("prompt") or {}).get("final") or payload.source
        source_type = "asset"
        media_type = "video"
    elif payload.source_type == "staged":
        raw = Path(payload.source)
        staging_root = (ROOT / "_staging").resolve()
        try:
            source = raw.resolve()
        except OSError:
            raise HTTPException(400, "路径无效")
        if staging_root not in source.parents:
            raise HTTPException(400, "只能处理暂存区里的文件，请先上传")
        if not source.is_file():
            raise HTTPException(404, "暂存文件已失效，请重新上传")
        suffix = source.suffix.lower()
        if suffix not in (watermark.VIDEO_EXTS | watermark.IMAGE_EXTS):
            raise HTTPException(400, "只支持视频或图片文件")
        label = source.name
        source_type = "staged"
        media_type = "image" if suffix in watermark.IMAGE_EXTS else "video"
    else:
        raise HTTPException(400, "source_type 只能是 asset 或 staged")

    # 图片与桌面版一致：固定 LAMA；只有 lama-direct 是额外的直接框选模式
    if media_type == "image" and mode not in ("lama", "lama-direct"):
        mode = "lama"

    job = watermark_manager.submit(
        source,
        source_type=source_type,
        label=str(label)[:80],
        media_type=media_type,
        mode=mode,
        detect_model=detect_model,
        preset=preset,
        engine=mode,
        areas=areas,
        ab=ab,
        auto_detect=bool(payload.auto_detect),
        hardware_acceleration=bool(payload.hardware_acceleration),
        advanced=advanced,
        detect_only=bool(payload.detect_only),
        box=payload.box,
        output_dir=WATERMARK_DIR / "outputs",
    )
    logger.info("开始去水印 | %s | 模式=%s | 检测=%s | 区域=%d | 自动=%s | 检测任务=%s",
                source.name, mode, detect_model, len(areas),
                payload.auto_detect, payload.detect_only)
    return job.to_dict()


@app.get("/api/watermark/jobs")
def watermark_jobs() -> dict[str, Any]:
    return {"jobs": [job.to_dict() for job in watermark_manager.active()]}


@app.get("/api/watermark/history")
def watermark_history() -> dict[str, Any]:
    return {"jobs": [job.to_dict() for job in watermark_manager.history()]}


@app.get("/api/watermark/{job_id}")
def watermark_status(job_id: str) -> dict[str, Any]:
    job = watermark_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job.to_dict()


@app.post("/api/watermark/{job_id}/cancel")
def watermark_cancel(job_id: str) -> dict[str, Any]:
    if not watermark_manager.cancel(job_id):
        raise HTTPException(400, "任务不存在或已经结束")
    return {"ok": True}


@app.delete("/api/watermark/{job_id}")
def watermark_forget(job_id: str) -> dict[str, Any]:
    """只删任务记录，不动成品文件。"""
    if not watermark_manager.forget(job_id):
        raise HTTPException(404, "任务不存在")
    return {"ok": True}


@app.get("/api/watermark/{job_id}/file")
def watermark_file(job_id: str) -> FileResponse:
    job = watermark_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.detect_only:
        raise HTTPException(404, "这是检测任务，没有成品文件")
    target = Path(job.output)
    if not target.exists():
        raise HTTPException(404, "结果文件不见了，可能被清理了")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, filename=target.name)


@app.get("/api/watermark/{job_id}/preview")
def watermark_preview(job_id: str) -> FileResponse:
    job = watermark_manager.get(job_id)
    if job is None or not job.preview:
        raise HTTPException(404, "没有检测预览图")
    target = Path(job.preview)
    if not target.exists():
        raise HTTPException(404, "检测预览图不见了")
    return FileResponse(target, media_type="image/jpeg", filename=target.name)


@app.get("/api/watermark/{job_id}/live-preview")
def watermark_live_preview(job_id: str) -> FileResponse:
    job = watermark_manager.get(job_id)
    if job is None or not job.live_preview:
        raise HTTPException(404, "没有处理预览图")
    target = Path(job.live_preview)
    if not target.exists():
        raise HTTPException(404, "处理预览图还没生成")
    return FileResponse(target, media_type="image/jpeg", filename=target.name)


@app.get("/api/watermark/{job_id}/log")
def watermark_log(job_id: str) -> FileResponse:
    job = watermark_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    target = job.log_path
    if not target.exists():
        raise HTTPException(404, "还没有运行日志")
    return FileResponse(target, media_type="text/plain; charset=utf-8", filename=target.name)


@app.post("/api/watermark/open-gui")
def watermark_open_gui() -> dict[str, Any]:
    """在本机打开逐帧框选的桌面版。"""
    if not watermark.VSR_PYTHON.exists() or not watermark.VSR_ROOT.exists():
        raise HTTPException(500, "找不到去水印工具目录，请检查 video-subtitle-remover 是否还在。")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [str(watermark.VSR_PYTHON), "gui.py"],
            cwd=str(watermark.VSR_ROOT),
            creationflags=flags,
            close_fds=True,
        )
    except OSError as exc:
        raise HTTPException(500, f"打开桌面版失败：{exc}")
    return {"ok": True}


@app.get("/api/models")
def get_models() -> dict[str, Any]:
    """模型资料 + 创作模式，给左上角的模型面板用。

    价格取自设置里的价格表（用户可改），所以改了价这里跟着变。
    """
    settings = config.load_settings()
    price_table = settings.get("pricing", {}).get("models", {})

    models: list[dict[str, Any]] = []
    for model_id in config.MODELS:
        info = dict(config.MODEL_INFO.get(model_id, {}))
        info["id"] = model_id
        tiers = price_table.get(model_id, {})
        info["pricing"] = [
            {
                "resolution": res,
                "list": float(tiers[res].get("list", 0)),
                "discount": float(tiers[res].get("discount", 1)),
                "actual": round(
                    float(tiers[res].get("list", 0)) * float(tiers[res].get("discount", 1)), 4
                ),
            }
            for res in ("480P", "720P", "1080P")
            if res in tiers
        ]
        models.append(info)

    return {
        "models": models,
        "modes": config.MODES,
        "current": settings.get("model"),
        "free_quota_seconds": settings.get("pricing", {}).get("free_quota_seconds"),
        "region": settings.get("region"),
    }


@app.get("/api/stats")
def get_stats() -> dict[str, Any]:
    """仪表盘数据。全部来自本地 meta.json，不调外部接口，所以随时能刷。"""
    settings = config.load_settings()
    return assets.stats(settings)


@app.get("/api/assets/{name}/refs")
def asset_refs(name: str) -> dict[str, Any]:
    """某个资产当初用的参考素材，带时长。

    「复用参数」时靠它把素材一起带回来——refs/ 里存着副本，不用用户重新上传。
    视频时长是现场探的，因为成本预估要把参考视频的秒数算进去。
    """
    settings = config.load_settings()
    try:
        job_dir = store.job_path(settings, name)
    except store.StoreError as exc:
        raise HTTPException(404, str(exc))

    meta = store.read_meta(job_dir) or {}
    return {"refs": assets.refs_with_duration(job_dir, meta)}


@app.get("/api/history/{name}/video")
def history_video(name: str) -> FileResponse:
    settings = config.load_settings()
    try:
        target = store.job_path(settings, name) / "video.mp4"
    except store.StoreError as exc:
        raise HTTPException(404, str(exc))
    if not target.exists():
        raise HTTPException(404, "这个任务没有视频文件")
    return FileResponse(target, media_type="video/mp4", filename="video.mp4")


@app.get("/api/history/{name}/meta")
def history_meta(name: str) -> dict[str, Any]:
    settings = config.load_settings()
    try:
        job_dir = store.job_path(settings, name)
    except store.StoreError as exc:
        raise HTTPException(404, str(exc))
    meta = store.read_meta(job_dir)
    if meta is None:
        raise HTTPException(404, "没有 meta.json")
    return meta


@app.get("/api/history/{name}/refs/{filename}")
def history_ref(name: str, filename: str) -> FileResponse:
    """参考素材副本。官方临时 URL 48 小时就失效，所以本地副本是唯一能追溯的来源。"""
    settings = config.load_settings()
    try:
        job_dir = store.job_path(settings, name)
    except store.StoreError as exc:
        raise HTTPException(404, str(exc))
    target = _safe_child(job_dir / "refs", filename)
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type)


# ---------------------------------------------------------------- 日志与诊断

@app.get("/api/logs")
def get_logs(lines: int = 400) -> dict[str, Any]:
    lines = max(50, min(int(lines), 5000))
    files = logging_setup.log_files()
    return {
        "lines": logging_setup.tail(lines),
        "file": str(logging_setup.LOG_FILE),
        "files": [
            {"name": f.name, "size_kb": round(f.stat().st_size / 1024, 1)}
            for f in files
        ],
        "exists": logging_setup.LOG_FILE.exists(),
    }


@app.get("/api/logs/download")
def download_log() -> FileResponse:
    if not logging_setup.LOG_FILE.exists():
        raise HTTPException(404, "还没有日志文件")
    return FileResponse(
        logging_setup.LOG_FILE, media_type="text/plain",
        filename=f"wan3-{datetime.now():%Y%m%d-%H%M%S}.log",
    )


@app.post("/api/logs/reveal")
def reveal_log() -> dict[str, Any]:
    """在文件管理器里打开日志目录。只有本机使用才有意义，所以直接调系统命令。"""
    target = logging_setup.LOG_DIR
    target.mkdir(parents=True, exist_ok=True)
    try:
        if platform.system() == "Windows":
            os.startfile(str(target))  # noqa: S606 - 本地工具，路径由代码决定
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except OSError as exc:
        raise HTTPException(500, f"打不开文件管理器：{exc}")
    return {"ok": True, "path": str(target)}


def _environment_report() -> str:
    lines = [
        f"生成时间      : {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"操作系统      : {platform.platform()}",
        f"Python        : {platform.python_version()} ({platform.python_implementation()})",
    ]

    try:
        import fastapi
        import uvicorn
        lines.append(f"fastapi       : {fastapi.__version__}")
        lines.append(f"uvicorn       : {uvicorn.__version__}")
    except Exception:  # noqa: BLE001
        pass

    if probe.ffprobe_available():
        try:
            out = subprocess.run(
                ["ffprobe", "-version"], capture_output=True, text=True, timeout=15
            )
            lines.append(f"ffprobe       : {out.stdout.splitlines()[0] if out.stdout else '已安装'}")
        except Exception:  # noqa: BLE001
            lines.append("ffprobe       : 已安装，但取版本失败")
    else:
        lines.append("ffprobe       : 未安装（素材规格预检会被跳过）")

    settings = config.load_settings()
    try:
        base_url = config.get_base_url(settings)
    except ValueError as exc:
        base_url = f"(配置有误) {exc}"

    lines += [
        "",
        "=== 当前配置（API Key 已隐去）===",
        f"地域          : {settings.get('region')}",
        f"接口地址      : {base_url}",
        f"接口风格      : {settings.get('endpoint_style')}",
        f"模型          : {settings.get('model')}",
        f"轮询间隔      : {settings.get('poll_interval_seconds')} 秒",
        f"已配置 Key    : {'是' if (settings.get('api_key') or '').strip() else '否'}",
        f"Key 指纹      : {config.mask_key(settings.get('api_key', '')) or '(未配置)'}",
    ]
    return "\n".join(lines)


@app.get("/api/diagnose/export")
def export_diagnostics() -> StreamingResponse:
    """导出一个诊断包。

    出问题时把这个 zip 发出来就够了：日志里有每次调用的 request_id，
    环境报告里有版本和配置（不含 Key），最近的请求体能证明当时到底发了什么。
    """
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("环境报告.txt", _environment_report())

        for log_file in logging_setup.log_files():
            try:
                bundle.write(log_file, f"logs/{log_file.name}")
            except OSError:
                continue

        # 配置去掉 Key 再打包
        settings = config.load_settings()
        settings["api_key"] = ""
        bundle.writestr(
            "settings.json",
            json.dumps(settings, ensure_ascii=False, indent=2),
        )

        # 最近 5 个任务的请求体与结果
        try:
            recent = store.list_jobs(settings)[:5]
        except Exception:  # noqa: BLE001
            recent = []

        for item in recent:
            try:
                job_dir = store.job_path(settings, item["dir"])
            except store.StoreError:
                continue
            for filename in ("request.json", "meta.json"):
                path = job_dir / filename
                if path.exists():
                    try:
                        bundle.write(path, f"jobs/{item['dir']}/{filename}")
                    except OSError:
                        continue

    buffer.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="wan3-diagnose-{stamp}.zip"'},
    )


# ---------------------------------------------------------------- 声音工作台

class VoiceProfileUpdate(BaseModel):
    name: str | None = None
    prompt_text: str | None = None
    notes: str | None = None


class VoiceTTSPayload(BaseModel):
    profile_id: str
    text: str
    mode: str = "instruct"
    instruct: str = ""
    speed: float = 1.0
    seed: int = 0
    variants: int = 1


class VoiceRevealPayload(BaseModel):
    path: str


@app.get("/api/voice/options")
def voice_options() -> dict[str, Any]:
    return {
        "emotions": voice.EMOTIONS,
        "modes": [
            {"value": "instruct", "label": "情绪指令", "desc": "用自然语言控制语气，推荐"},
            {"value": "zero_shot", "label": "原声复刻", "desc": "按参考台词复刻，需要参考台词"},
        ],
        "model": voice.TTS_MODEL,
        "limits": {
            "max_text": 500,
            "max_variants": 3,
            "min_reference_seconds": voice.MIN_REFERENCE_SECONDS,
            "recommended_reference_seconds": voice.RECOMMENDED_REFERENCE_SECONDS,
            "max_reference_seconds": voice.MAX_REFERENCE_SECONDS,
        },
    }


@app.get("/api/voice/status")
def voice_status() -> dict[str, Any]:
    return voice_service.status(config.load_settings())


@app.post("/api/voice/service/start")
def voice_service_start() -> dict[str, Any]:
    settings = config.load_settings()
    try:
        return voice_service.start(settings)
    except voice_service.VoiceServiceError as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/voice/service/stop")
def voice_service_stop() -> dict[str, Any]:
    return voice_service.stop(config.load_settings())


@app.post("/api/voice/service/unload")
def voice_service_unload() -> dict[str, Any]:
    settings = config.load_settings()
    try:
        return voice_service.unload_model(settings)
    except voice_service.VoiceServiceError as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/voice/profiles")
def voice_profiles() -> dict[str, Any]:
    settings = config.load_settings()
    profiles = voice.load_profiles(settings)
    for profile in profiles:
        profile["reference_url"] = f"/api/voice/profiles/{profile['id']}/reference"
    return {"profiles": profiles}


@app.get("/api/voice/profiles/{profile_id}/reference")
def voice_profile_reference(profile_id: str) -> FileResponse:
    settings = config.load_settings()
    try:
        profile = voice.find_profile(settings, profile_id)
        path = voice.reference_path(settings, profile)
    except voice.VoiceError as exc:
        raise HTTPException(404, str(exc))
    return FileResponse(path, media_type="audio/wav", filename=f"{profile.get('name', 'reference')}.wav")


@app.post("/api/voice/profiles")
async def voice_profile_create(
    name: str = Form(...),
    prompt_text: str = Form(""),
    notes: str = Form(""),
    files: list[UploadFile] | None = File(default=None),
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    settings = config.load_settings()
    uploads = list(files or [])
    if file is not None:
        uploads.insert(0, file)
    if not uploads:
        raise HTTPException(400, "至少上传一个参考音频或视频")

    token = uuid.uuid4().hex[:12]
    temp_dir = STAGING_DIR / f"voice_{token}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    targets: list[Path] = []
    try:
        for index, upload in enumerate(uploads, 1):
            original = Path(upload.filename or f"reference_{index}.bin").name
            target_dir = temp_dir / str(index)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / original
            with target.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    handle.write(chunk)
            if target.stat().st_size == 0:
                raise HTTPException(400, f"第 {index} 个文件为空")
            targets.append(target)
        profile = voice.create_profile(
            settings,
            name=name,
            prompt_text=prompt_text,
            notes=notes,
            sources=targets,
        )
    except voice.VoiceError as exc:
        raise HTTPException(400, str(exc))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    profile["reference_url"] = f"/api/voice/profiles/{profile['id']}/reference"
    return {"profile": profile}


@app.patch("/api/voice/profiles/{profile_id}")
def voice_profile_update(profile_id: str, payload: VoiceProfileUpdate) -> dict[str, Any]:
    settings = config.load_settings()
    try:
        profile = voice.update_profile(
            settings, profile_id, **payload.model_dump(exclude_none=True)
        )
    except voice.VoiceError as exc:
        raise HTTPException(404, str(exc))
    profile["reference_url"] = f"/api/voice/profiles/{profile['id']}/reference"
    return {"profile": profile}


@app.delete("/api/voice/profiles/{profile_id}")
def voice_profile_delete(profile_id: str) -> dict[str, Any]:
    settings = config.load_settings()
    try:
        voice.delete_profile(settings, profile_id)
    except voice.VoiceError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


@app.post("/api/voice/profiles/{profile_id}/transcribe")
def voice_profile_transcribe(profile_id: str) -> dict[str, Any]:
    settings = config.load_settings()
    try:
        text = voice.transcribe_profile(settings, profile_id)
    except voice.VoiceError as exc:
        raise HTTPException(400, str(exc))
    return {"text": text}


@app.post("/api/voice/tts")
def voice_tts(payload: VoiceTTSPayload) -> dict[str, Any]:
    settings = config.load_settings()
    try:
        job = voice.create_tts_job(settings, **payload.model_dump())
    except voice.VoiceError as exc:
        raise HTTPException(400, str(exc))
    except voice_service.VoiceServiceError as exc:
        raise HTTPException(500, str(exc))
    return {"job": job}


@app.get("/api/voice/history")
def voice_history(limit: int = 50) -> dict[str, Any]:
    settings = config.load_settings()
    return {"jobs": voice.list_history(settings, limit=limit)}


@app.get("/api/voice/audio/{job_id}/{filename}")
def voice_audio(job_id: str, filename: str) -> FileResponse:
    settings = config.load_settings()
    try:
        path = voice.job_audio_path(settings, job_id, filename)
    except voice.VoiceError as exc:
        raise HTTPException(404, str(exc))
    media = "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg"
    return FileResponse(path, media_type=media, filename=filename)


@app.delete("/api/voice/history/{job_id}")
def voice_history_delete(job_id: str) -> dict[str, Any]:
    settings = config.load_settings()
    try:
        voice.delete_job(settings, job_id)
    except voice.VoiceError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


@app.post("/api/voice/reveal")
def voice_reveal(payload: VoiceRevealPayload) -> dict[str, Any]:
    settings = config.load_settings()
    try:
        voice.reveal(Path(payload.path), settings)
    except voice.VoiceError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


# ---------------------------------------------------------------- 静态页面

@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.exception_handler(Exception)
async def unhandled(_, exc: Exception) -> JSONResponse:
    logger.error("未捕获的服务端错误：%s", exc)
    return JSONResponse(status_code=500, content={"detail": f"服务端错误：{exc}"})


def _cleanup_staging() -> None:
    """清理过期暂存文件，避免 _staging 无限增长。"""
    cutoff = time.time() - STAGING_TTL_SECONDS
    for entry in STAGING_DIR.iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


if __name__ == "__main__":
    import uvicorn

    logging_setup.setup_logging()
    _cleanup_staging()

    print("=" * 58)
    print("  万相3.0 视频创作台")
    print("  浏览器打开：http://127.0.0.1:8000")
    print(f"  日志文件：{logging_setup.LOG_FILE}")
    print("  按 Ctrl+C 停止")
    print("=" * 58)

    logger.info("服务启动 | Python %s | %s", platform.python_version(), platform.platform())
    logger.info("日志文件位置：%s", logging_setup.LOG_FILE)

    # 把上次进程退出时正在跑的任务接回来。
    # 任务提交成功后由阿里那边在跑（task_id 24 小时有效），但本地轮询线程
    # 随进程一起没了 —— 不接回来用户就会一直等一个永远不会到的视频。
    # 放后台线程里做，别挡住启动。
    def _resume() -> None:
        try:
            resumed = manager.resume_orphaned_jobs(config.load_settings())
            if resumed:
                logger.info("已恢复 %d 个中断的任务：%s", len(resumed), ", ".join(resumed))
        except Exception as exc:  # noqa: BLE001
            logger.error("恢复中断任务时出错：%s", exc)

    threading.Thread(target=_resume, daemon=True).start()

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
