"""去掉图像的透明通道。

为什么需要这个：官方对输入图像的硬性要求写明「PNG（不支持透明通道）」，
而 Windows 的屏幕截图（Win+Shift+S）默认就存成带 alpha 的 RGBA PNG。
也就是说，用截图当参考图的人**每次**都会撞上这条规则。

处理方式是把透明区域**压平到白底**，而不是直接丢掉 alpha 通道——
丢掉的话，原本透明的像素会变成黑色或者花屏，画面对不上。

只改暂存区里的副本，不动用户的原文件。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

try:
    from PIL import Image

    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - 环境缺 Pillow 时降级
    Image = None  # type: ignore[assignment]
    PILLOW_AVAILABLE = False

# 只有这几种格式可能带 alpha
ALPHA_CAPABLE_SUFFIXES = {".png", ".webp", ".bmp"}

# 压平用的底色。白色是最不会出意外的选择。
BACKGROUND_COLOR = (255, 255, 255)


def has_alpha(path: Path) -> bool:
    """判断图像是否真的带透明通道。

    比看 ffprobe 的 pix_fmt 更准：P 模式（调色板）的 PNG 透明信息藏在
    info["transparency"] 里，pix_fmt 看不出来。
    """
    if not PILLOW_AVAILABLE:
        return False

    path = Path(path)
    if path.suffix.lower() not in ALPHA_CAPABLE_SUFFIXES:
        return False

    try:
        with Image.open(path) as image:
            if image.mode in ("RGBA", "LA", "PA"):
                return True
            if image.mode == "P" and "transparency" in image.info:
                return True
            return False
    except (OSError, ValueError):
        return False


def flatten(
    path: Path, *, output: Path | None = None, keep_backup: bool = True
) -> dict[str, Any]:
    """把带透明通道的图像压平到白底，返回处理结果。

    默认就地覆盖（暂存区副本，不是原文件）。输出统一为 PNG，因为
    JPEG 本来就不支持 alpha，重新编码反而会损失画质。
    """
    path = Path(path)

    result: dict[str, Any] = {
        "applied": False,
        "pillow_available": PILLOW_AVAILABLE,
        "source": path.name,
    }

    if not PILLOW_AVAILABLE:
        result["reason"] = "本机没有装 Pillow，无法自动处理"
        return result

    if not path.exists():
        result["reason"] = "文件不存在"
        return result

    if not has_alpha(path):
        result["reason"] = "本来就没有透明通道，无需处理"
        return result

    try:
        with Image.open(path) as image:
            original_mode = image.mode
            size = image.size
            rgba = image.convert("RGBA")

            # 用 alpha 通道当蒙版，把图像贴到纯白底上
            canvas = Image.new("RGB", rgba.size, BACKGROUND_COLOR)
            canvas.paste(rgba, mask=rgba.split()[-1])

            target = Path(output) if output else path
            if keep_backup and target == path:
                backup = path.with_suffix(path.suffix + ".original")
                if not backup.exists():
                    shutil.copy2(path, backup)

            canvas.save(target, format="PNG", optimize=True)

    except (OSError, ValueError) as exc:
        result["reason"] = f"处理失败：{exc}"
        return result

    result.update(
        {
            "applied": True,
            "output": str(target),
            "output_name": target.name,
            "original_mode": original_mode,
            "width": size[0],
            "height": size[1],
            # 文件名变了的话，调用方要跟着更新路径
            "path_changed": target != path,
        }
    )
    return result


def ensure_no_alpha(path: Path) -> tuple[Path, dict[str, Any]]:
    """给上传流程用的入口：需要就压平，返回（可用的路径, 处理信息）。

    不需要处理时原样返回，调用方不用做分支判断。
    """
    path = Path(path)
    info = flatten(path)
    if info.get("applied") and info.get("path_changed"):
        return Path(info["output"]), info
    return path, info
