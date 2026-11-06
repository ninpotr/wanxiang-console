"""素材组合规则与任务模式判定。

官方文档「素材组合」一节写得很死，违反任意一条接口直接报错：

  - first_frame / last_frame 与 reference_image / reference_video /
    reference_audio / file / link **互斥**，不能在同一请求里混用。
    典型报错："The two modes are mutually exclusive. Do not pass reference_xx
    and first_frame/last_frame at the same time."
  - file 和 link 二选一，但都可以和 reference_* 组合。
  - reference_image / reference_video / reference_audio 之间可自由组合。
  - 文生视频：只传 prompt，不支持传 media。

这个模块把这些规则集中在一处，界面上做前置校验，避免提交后才发现白排一次队。
"""

from __future__ import annotations

from typing import Any

FRAME_TYPES = {"first_frame", "last_frame"}
REFERENCE_TYPES = {"reference_image", "reference_video", "reference_audio"}
DOC_TYPES = {"file", "link"}

MEDIA_TYPE_LABELS = {
    "first_frame": "首帧图像",
    "last_frame": "尾帧图像",
    "reference_image": "参考图像",
    "reference_video": "参考视频",
    "reference_audio": "参考音频",
    "file": "文件",
    "link": "网页链接",
}

# 文件/网页素材强制开启智能改写（官方硬性要求）
PROMPT_EXTEND_REQUIRED_TYPES = DOC_TYPES


def validate_combo(media_types: list[str]) -> list[str]:
    """校验一组 media type 是否构成官方允许的组合，返回错误列表。"""
    errors: list[str] = []
    present = set(media_types)

    unknown = present - set(MEDIA_TYPE_LABELS)
    if unknown:
        errors.append(f"未知的素材类型：{', '.join(sorted(unknown))}")

    frames = present & FRAME_TYPES
    others = present - FRAME_TYPES

    if frames and others:
        conflicts = "、".join(
            MEDIA_TYPE_LABELS.get(t, t) for t in sorted(others)
        )
        errors.append(
            f"首帧/尾帧与参考类素材互斥，不能同时使用。"
            f"当前同时选了：{'、'.join(MEDIA_TYPE_LABELS.get(t, t) for t in sorted(frames))}"
            f" 和 {conflicts}。"
        )

    if "file" in present and "link" in present:
        errors.append("文件和网页链接只能二选一，不能同时传入。")

    if "last_frame" in present and "first_frame" not in present:
        errors.append("传了尾帧就必须同时传首帧，不能只给尾帧。")

    return errors


def derive_mode(media_types: list[str]) -> str:
    """判定任务模式，仅用于界面显示。"""
    present = set(media_types)
    if not present:
        return "文生视频"
    if present & FRAME_TYPES:
        return "首尾帧生视频" if "last_frame" in present else "首帧生视频"
    if "file" in present or "link" in present:
        return "文件/网页生视频"
    return "参考生视频"


def prompt_extend_locked(media_types: list[str]) -> tuple[bool, str]:
    """判断 prompt_extend 是否被强制锁定为 true。

    官方原文：「当传入文档（file）或网页（link）素材时，prompt_extend 取值必须为 true。」
    """
    locked = bool(set(media_types) & PROMPT_EXTEND_REQUIRED_TYPES)
    reason = (
        "传入文件或网页素材时，官方要求必须开启智能改写，此开关已锁定。"
        if locked
        else ""
    )
    return locked, reason


def validate_duration(
    duration: int,
    input_video_seconds: float,
    *,
    min_seconds: int = 2,
    max_seconds: int = 30,
    total_limit: int = 30,
    smart_value: int = -1,
) -> list[str]:
    """时长相关的校验，含「有视频输入时 输入+输出 <= 30 秒」这条。"""
    errors: list[str] = []

    if duration == smart_value:
        return errors

    if not min_seconds <= duration <= max_seconds:
        errors.append(f"时长必须在 {min_seconds}~{max_seconds} 秒之间，或使用智能时长。")
        return errors

    if input_video_seconds > 0:
        if input_video_seconds + duration > total_limit:
            allowed = int(total_limit - input_video_seconds)
            errors.append(
                f"输入视频 {input_video_seconds:.1f} 秒 + 输出 {duration} 秒 = "
                f"{input_video_seconds + duration:.1f} 秒，超过 {total_limit} 秒上限。"
                f"有视频输入时，本条的输出时长最多 {allowed} 秒。"
            )
    return errors


def build_media_payload(
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把素材整理成接口要的 media 数组。

    顺序很重要：官方按数组顺序定义编号，第 1 个 reference_image 就是「图1」，
    第 1 个 reference_video 就是「视频1」，图和视频分别计数。
    所以这里保持用户的上传顺序，不做重排。
    """
    media: list[dict[str, Any]] = []
    for asset in assets:
        url = asset.get("url")
        media_type = asset.get("media_type")
        if not url or not media_type:
            continue
        media.append({"type": media_type, "url": url})
    return media


def summarize_media(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给 prompt_opt 用的素材清单：编号 + 类型 + 用户说明，不含内容。"""
    counters = {"image": 0, "video": 0, "audio": 0}
    labels = {"image": "图", "video": "视频", "audio": "音频"}

    manifest: list[dict[str, Any]] = []
    for asset in assets:
        kind = asset.get("kind")
        if kind not in counters:
            continue
        # 官方按模态分别计数：图1、图2、视频1……
        media_type = asset.get("media_type") or ""
        if media_type in ("first_frame", "last_frame"):
            label = {"first_frame": "首帧", "last_frame": "尾帧"}[media_type]
        else:
            counters[kind] += 1
            label = f"{labels[kind]}{counters[kind]}"
        manifest.append(
            {
                "label": label,
                "kind": kind,
                "name": asset.get("name", ""),
                "note": asset.get("note", ""),
            }
        )
    return manifest
