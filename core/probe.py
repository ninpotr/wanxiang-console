"""参考素材预检。

官方对输入素材有硬性限制，提交后被拒的话这条任务就白排一次队。
本机有 ffprobe，所以在提交前把不合规的地方直接指出来。

限制来源：万相3.0-视频生成API参考「传入图像/视频/音频」三节。

总时长限制（视频合计 <=15 秒、音频合计 <=15 秒）需要把同一批素材放一起判断，
所以放在 validate_batch()。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXTS = {".mp4", ".mov"}
AUDIO_EXTS = {".wav", ".mp3"}

IMAGE_MAX_MB = 20
VIDEO_MAX_MB = 100
AUDIO_MAX_MB = 15
SIDE_MIN = 240
SIDE_MAX = 8000
VIDEO_SIDE_MAX = 4096
ASPECT_MAX = 8.0
VIDEO_MIN_FPS = 16.0
CLIP_MIN_SECONDS = 1.0
CLIP_MAX_SECONDS = 15.0
REFERENCE_VIDEO_TOTAL_SECONDS = 15.0
REFERENCE_AUDIO_TOTAL_SECONDS = 15.0
REFERENCE_IMAGE_MAX_COUNT = 10
REFERENCE_VIDEO_MAX_COUNT = 5
REFERENCE_AUDIO_MAX_COUNT = 5

_FFPROBE = shutil.which("ffprobe")


def ffprobe_available() -> bool:
    return _FFPROBE is not None


def _run_ffprobe(path: Path) -> dict[str, Any] | None:
    if _FFPROBE is None:
        return None
    cmd = [
        _FFPROBE,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace"
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def detect_kind(path: Path) -> str:
    """按扩展名判断素材类别：image / video / audio / unknown。"""
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "unknown"


def _fraction_to_float(value: str | None) -> float:
    if not value or value in ("0/0", "N/A"):
        return 0.0
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def inspect(path: Path) -> dict[str, Any]:
    """探测单个文件，返回规格信息 + 逐条校验结果。

    校验结果分 errors（一定失败）和 warnings（可能失败或影响效果），
    界面按这个分级展示。
    """
    kind = detect_kind(path)
    size_mb = path.stat().st_size / (1024 * 1024) if path.exists() else 0.0

    info: dict[str, Any] = {
        "name": path.name,
        "path": str(path),
        "kind": kind,
        "size_mb": round(size_mb, 2),
        "ffprobe": ffprobe_available(),
    }
    errors: list[str] = []
    warnings: list[str] = []

    if not path.exists():
        return {**info, "ok": False, "errors": ["文件不存在"], "warnings": []}

    if kind == "unknown":
        errors.append(
            f"不支持的扩展名 {path.suffix}。"
            "图片支持 JPEG/JPG/PNG/BMP/WEBP，视频支持 mp4/mov，音频支持 wav/mp3。"
        )
        return {**info, "ok": False, "errors": errors, "warnings": warnings}

    # 文件大小按类别分别校验
    limit = {"image": IMAGE_MAX_MB, "video": VIDEO_MAX_MB, "audio": AUDIO_MAX_MB}[kind]
    if size_mb > limit:
        errors.append(f"文件 {size_mb:.1f}MB 超过 {limit}MB 上限")
    info["size_limit_mb"] = limit

    probed = _run_ffprobe(path)
    if probed is None:
        if ffprobe_available():
            warnings.append("ffprobe 读取失败，跳过规格校验（文件可能损坏）")
        else:
            warnings.append("本机没装 ffprobe，跳过规格校验")
        return {**info, "ok": not errors, "errors": errors, "warnings": warnings}

    streams = probed.get("streams", [])
    fmt = probed.get("format", {})

    duration = _fraction_to_float(fmt.get("duration"))
    if duration <= 0:
        for stream in streams:
            duration = duration or _fraction_to_float(stream.get("duration"))
    if duration > 0:
        info["duration"] = round(duration, 2)

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video_stream:
        width = int(video_stream.get("width") or 0)
        height = int(video_stream.get("height") or 0)
        fps = _fraction_to_float(
            video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
        )
        pix_fmt = (video_stream.get("pix_fmt") or "").lower()
        info.update({"width": width, "height": height, "fps": round(fps, 2)})

        if width and height:
            shorter, longer = sorted((width, height))
            if shorter < SIDE_MIN:
                errors.append(f"短边 {shorter}px 小于 {SIDE_MIN}px")
            side_max = SIDE_MAX if kind == "image" else VIDEO_SIDE_MAX
            if longer > side_max:
                errors.append(f"长边 {longer}px 超过 {side_max}px")
            if shorter:
                ratio = longer / shorter
                info["aspect"] = round(ratio, 3)
                if ratio > ASPECT_MAX:
                    errors.append(f"长宽比 {ratio:.1f}:1 超过 {ASPECT_MAX}:1 上限")

        if kind == "image":
            # 官方明确：PNG 不支持透明通道。
            # 正常情况下 app.py 在暂存阶段已经自动压平过了，走到这里说明自动修复
            # 没生效（多半是本机没装 Pillow），所以错误信息里直接给替代办法。
            alpha_formats = ("rgba", "bgra", "argb", "abgr", "ya8", "ya16", "pal8", "gbrap")
            if any(flag in pix_fmt for flag in alpha_formats):
                errors.append(
                    f"图像含透明通道（{pix_fmt}），官方不支持。"
                    "自动修复没生效，请用画图或 Photoshop 另存为 JPG 再上传，"
                    "或在本机执行 pip install Pillow 后重启服务。"
                )

        if kind == "video":
            if fps and fps < VIDEO_MIN_FPS:
                errors.append(f"帧率 {fps:.1f}fps 低于 {VIDEO_MIN_FPS}fps 下限")
            if duration > 0:
                if duration < CLIP_MIN_SECONDS:
                    errors.append(f"时长 {duration:.1f} 秒短于 {CLIP_MIN_SECONDS} 秒下限")
                if duration > CLIP_MAX_SECONDS:
                    errors.append(
                        f"单个视频 {duration:.1f} 秒超过 {CLIP_MAX_SECONDS} 秒上限，请先裁剪"
                    )

    if kind == "audio":
        if audio_stream is None and video_stream is None:
            errors.append("没检测到音频流")
        if duration > 0:
            if duration < CLIP_MIN_SECONDS:
                errors.append(f"时长 {duration:.1f} 秒短于 {CLIP_MIN_SECONDS} 秒下限")
            if duration > CLIP_MAX_SECONDS:
                errors.append(
                    f"单个音频 {duration:.1f} 秒超过 {CLIP_MAX_SECONDS} 秒上限，请先裁剪"
                )

    return {**info, "ok": not errors, "errors": errors, "warnings": warnings}


def validate_batch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量校验：补上「合计时长」和「数量上限」这两条跨素材的约束。

    items 里每项是 inspect() 的返回值，再带上将要使用的 type。
    """
    results = [dict(item) for item in items]
    for result in results:
        result.setdefault("errors", [])
        result.setdefault("warnings", [])

    def _pick(kind: str, media_type: str) -> list[dict[str, Any]]:
        return [r for r in results if r.get("kind") == kind and r.get("media_type") == media_type]

    ref_videos = _pick("video", "reference_video")
    if len(ref_videos) > REFERENCE_VIDEO_MAX_COUNT:
        for r in ref_videos:
            r["errors"].append(
                f"参考视频最多 {REFERENCE_VIDEO_MAX_COUNT} 段，当前 {len(ref_videos)} 段"
            )
    total_video = sum(float(r.get("duration") or 0) for r in ref_videos)
    if total_video > REFERENCE_VIDEO_TOTAL_SECONDS:
        for r in ref_videos:
            r["errors"].append(
                f"参考视频合计 {total_video:.1f} 秒，超过 {REFERENCE_VIDEO_TOTAL_SECONDS} 秒上限"
            )

    ref_audios = _pick("audio", "reference_audio")
    if len(ref_audios) > REFERENCE_AUDIO_MAX_COUNT:
        for r in ref_audios:
            r["errors"].append(
                f"参考音频最多 {REFERENCE_AUDIO_MAX_COUNT} 段，当前 {len(ref_audios)} 段"
            )
    total_audio = sum(float(r.get("duration") or 0) for r in ref_audios)
    if total_audio > REFERENCE_AUDIO_TOTAL_SECONDS:
        for r in ref_audios:
            r["errors"].append(
                f"参考音频合计 {total_audio:.1f} 秒，超过 {REFERENCE_AUDIO_TOTAL_SECONDS} 秒上限"
            )

    ref_images = _pick("image", "reference_image")
    if len(ref_images) > REFERENCE_IMAGE_MAX_COUNT:
        for r in ref_images:
            r["errors"].append(
                f"参考图片最多 {REFERENCE_IMAGE_MAX_COUNT} 张，当前 {len(ref_images)} 张"
            )

    # 首尾帧最多各一张
    for frame_type, label in (("first_frame", "首帧"), ("last_frame", "尾帧")):
        frames = _pick("image", frame_type)
        if len(frames) > 1:
            for r in frames:
                r["errors"].append(f"{label}图像最多 1 张，当前 {len(frames)} 张")

    for result in results:
        result["ok"] = not result["errors"]

    return results
