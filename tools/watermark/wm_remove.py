# -*- coding: utf-8 -*-
"""
AI 生成水印去除工具（豆包 / LibLib / 万相 / 即梦 / 可灵 / 海螺 等）

原理：先用 OCR 在画面的四角/边缘自动找出“每帧都在同一位置”的文字水印，
再用 video-subtitle-remover 内置的 STTN / LAMA 模型只重绘水印区域。
也可以手动给出水印框，完全跳过自动检测。

示例：
    # 自动检测 + STTN（视频，速度快、时序稳定）
    python wm_remove.py -i input.mp4 --preset auto

    # 已知平台，直接用预设框
    python wm_remove.py -i input.mp4 --preset doubao

    # 手动指定相对坐标（x1 y1 x2 y2，取值 0~1）
    python wm_remove.py -i input.mp4 --box 0.75 0.90 1.0 1.0

    # 图片（自动使用 LAMA）
    python wm_remove.py -i photo.jpg --preset auto

    # 批量处理整个文件夹
    python wm_remove.py -i ./inputs -o ./outputs --preset auto

    # 只检测并输出预览图，不执行去除
    python wm_remove.py -i input.mp4 --preset auto --detect-only
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
from tqdm import tqdm

HERE = Path(__file__).resolve().parent
# 默认：控制台目录的上一级是仓库根，VSR 与仓库同级。
# 可以用 WAN3_VSR_ROOT 指定 video-subtitle-remover 的实际位置。
VSR_ROOT = Path(
    os.environ.get("WAN3_VSR_ROOT")
    or (HERE.parents[2] / "video-subtitle-remover")
)
ROOT = HERE
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(VSR_ROOT))

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".wmv", ".ts"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

# 相对坐标 (x1, y1, x2, y2)，取值 0~1。
PRESETS = {
    # 豆包：右下角 “豆包AI生成” 标识
    "doubao": (0.66, 0.885, 1.0, 1.0),
    # LibLib / 哩布哩布 / libtv：右下角 logo
    "liblib": (0.74, 0.905, 1.0, 1.0),
    "libtv": (0.72, 0.900, 1.0, 1.0),
    # 通义万相：右下角“通义万相” logo
    "wanxiang": (0.74, 0.895, 1.0, 1.0),
    "wanx": (0.74, 0.895, 1.0, 1.0),
    # 即梦
    "jimeng": (0.72, 0.900, 1.0, 1.0),
    # 可灵
    "kling": (0.72, 0.900, 1.0, 1.0),
    # 海螺 / MiniMax
    "hailuo": (0.72, 0.900, 1.0, 1.0),
    # 通用角落兜底
    "generic-br": (0.70, 0.885, 1.0, 1.0),
    "generic-bl": (0.0, 0.885, 0.30, 1.0),
    "generic-tr": (0.70, 0.0, 1.0, 0.115),
    "generic-tl": (0.0, 0.0, 0.30, 0.115),
    "generic-top": (0.0, 0.0, 1.0, 0.115),
    "generic-bottom": (0.0, 0.885, 1.0, 1.0),
}

# 自动检测时允许搜索的画面区域（相对坐标）
SEARCH_REGIONS = [
    (0.0, 0.78, 0.42, 1.0),    # 左下
    (0.58, 0.78, 1.0, 1.0),    # 右下
    (0.0, 0.0, 0.42, 0.22),    # 左上
    (0.58, 0.0, 1.0, 0.22),    # 右上
]


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

_PROGRESS_ENABLED = False
_PROGRESS_BASE = 0.0
_PROGRESS_SPAN = 100.0


def log(msg: str) -> None:
    print(msg, flush=True)


def progress(pct: float, stage: str = "", **extra) -> None:
    """给网页控制台用的机器可读进度；普通命令行下完全静默。"""
    if not _PROGRESS_ENABLED:
        return
    local = max(0.0, min(100.0, float(pct)))
    payload = {
        "progress": round(_PROGRESS_BASE + local * _PROGRESS_SPAN / 100.0, 1),
        "stage": stage,
    }
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    print("@@WM_PROGRESS " + json.dumps(payload, ensure_ascii=False), flush=True)


def rel_box_to_abs(box, width: int, height: int):
    x1, y1, x2, y2 = box
    ax1 = int(round(max(0.0, min(1.0, x1)) * width))
    ax2 = int(round(max(0.0, min(1.0, x2)) * width))
    ay1 = int(round(max(0.0, min(1.0, y1)) * height))
    ay2 = int(round(max(0.0, min(1.0, y2)) * height))
    ax1, ax2 = max(0, min(ax1, ax2)), min(width, max(ax1, ax2))
    ay1, ay2 = max(0, min(ay1, ay2)), min(height, max(ay1, ay2))
    return ax1, ay1, ax2, ay2


def abs_box_to_rel(box, width: int, height: int):
    x1, y1, x2, y2 = box
    return (x1 / width, y1 / height, x2 / width, y2 / height)


def format_box(box):
    return "({:.3f}, {:.3f}, {:.3f}, {:.3f})".format(*box)


def iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


def box_center(b):
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def union_box(boxes):
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def expand_corner_box(box, width, height):
    """角落水印：朝画面外侧多给一点余量，用来覆盖紧挨文字的 logo。"""
    x1, y1, x2, y2 = box
    cx = (x1 + x2) / 2 / max(1, width)
    cy = (y1 + y2) / 2 / max(1, height)
    # 平台水印常常是「logo + 文字」，OCR 只框到文字；朝画面内侧多留一点，
    # 否则紧挨文字左侧/上方的 logo 会残留一条边。
    pad_in_x = max(10, int(width * 0.032))
    pad_out_x = max(8, int(width * 0.030))
    pad_in_y = max(8, int(height * 0.022))
    pad_out_y = max(6, int(height * 0.024))

    if cx < 0.5:
        nx1 = max(0, x1 - pad_out_x)
        nx2 = min(width, x2 + pad_in_x)
    else:
        nx1 = max(0, x1 - pad_in_x)
        nx2 = min(width, x2 + pad_out_x)

    if cy < 0.5:
        ny1 = max(0, y1 - pad_out_y)
        ny2 = min(height, y2 + pad_in_y)
    else:
        ny1 = max(0, y1 - pad_in_y)
        ny2 = min(height, y2 + pad_out_y)
    return nx1, ny1, nx2, ny2


def merge_nearby_boxes(boxes, width, height):
    """把同一处水印被 OCR 拆成多块（logo + 文字）的框并起来。"""
    boxes = [tuple(map(int, b)) for b in boxes]
    gap_x = max(8, int(width * 0.06))
    gap_y = max(6, int(height * 0.02))

    def overlap_1d(a1, a2, b1, b2):
        return max(0, min(a2, b2) - max(a1, b1))

    changed = True
    while changed and len(boxes) > 1:
        changed = False
        result = []
        used = [False] * len(boxes)
        for i, a in enumerate(boxes):
            if used[i]:
                continue
            cur = a
            for j in range(i + 1, len(boxes)):
                if used[j]:
                    continue
                b = boxes[j]
                # 横向相邻且纵向有明显重叠
                horizontal_gap = max(0, max(cur[0], b[0]) - min(cur[2], b[2]))
                vertical_overlap = overlap_1d(cur[1], cur[3], b[1], b[3])
                # 纵向相邻且横向有明显重叠
                vertical_gap = max(0, max(cur[1], b[1]) - min(cur[3], b[3]))
                horizontal_overlap = overlap_1d(cur[0], cur[2], b[0], b[2])
                min_h = max(1, min(cur[3] - cur[1], b[3] - b[1]))
                min_w = max(1, min(cur[2] - cur[0], b[2] - b[0]))

                if (horizontal_gap <= gap_x and vertical_overlap >= 0.35 * min_h) or \
                   (vertical_gap <= gap_y and horizontal_overlap >= 0.35 * min_w):
                    cur = union_box([cur, b])
                    used[j] = True
                    changed = True
            used[i] = True
            result.append(cur)
        boxes = result
    return boxes


# ---------------------------------------------------------------------------
# 自动检测水印位置
# ---------------------------------------------------------------------------

_OCR_DETECTOR = None


def _load_backend_config(interface="ch", ocr_model="mobile"):
    """按 CLI 的方式初始化 backend.config / 翻译 / OCR 模型选择。"""
    from backend.config import config, tr, BASE_DIR
    from backend.tools.constant import SubtitleDetectMode

    config.set(config.interface, interface)
    tr.read(os.path.join(BASE_DIR, "interface", f"{interface}.ini"), encoding="utf-8")

    if ocr_model == "server":
        mode = SubtitleDetectMode.PP_OCRv5_SERVER
    else:
        mode = SubtitleDetectMode.PP_OCRv5_MOBILE
    config.set(config.subtitleDetectMode, mode)
    return config, tr


def _get_ocr_detector():
    """批量处理时复用同一个 OCR 模型，避免每个文件都重新加载。"""
    global _OCR_DETECTOR
    if _OCR_DETECTOR is None:
        from backend.tools.subtitle_detect import SubtitleDetect

        helper = SubtitleDetect.__new__(SubtitleDetect)
        helper.video_path = ""
        helper.sub_areas = []
        helper.SAMPLE_STEP = 1
        _OCR_DETECTOR = helper.text_detector
    return _OCR_DETECTOR


def _ocr_text_boxes(detector, frame):
    """检测一帧里的所有文字框，返回 (x1, y1, x2, y2)。"""
    from backend.tools.ocr import get_coordinates

    boxes = []
    for res in detector.predict(frame):
        dt_polys = res.get("dt_polys")
        if dt_polys is None or len(dt_polys) == 0:
            continue
        for xmin, xmax, ymin, ymax in get_coordinates(dt_polys.tolist()):
            boxes.append((xmin, ymin, xmax, ymax))
    return boxes


def _load_sample_frames(path: str, is_image: bool, max_frames=8):
    """均匀抽取用于检测的帧。图片返回单帧。"""
    if is_image:
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"无法读取图片：{path}")
        return [img], img.shape[1], img.shape[0], []

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频：{path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    if total <= 0:
        # 某些容器拿不到总帧数，退化为顺序读取
        frames = []
        step = max(1, int(fps))
        idx = 0
        while len(frames) < max_frames:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % step == 0:
                frames.append(frame)
            idx += 1
        cap.release()
        return frames, width, height, []

    n = min(max_frames, max(1, total))
    indices = sorted(set(int(round(i * (total - 1) / max(1, n - 1))) for i in range(n)))
    frames = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if ok:
            frames.append(frame)
    cap.release()
    return frames, width, height, indices


def _filter_corner_boxes(boxes, width, height):
    """保留四角/边缘、尺寸像水印的文字框。"""
    kept = []
    for b in boxes:
        x1, y1, x2, y2 = b
        bw, bh = x2 - x1, y2 - y1
        if bw <= 2 or bh <= 2:
            continue
        w_norm, h_norm = bw / width, bh / height
        if w_norm > 0.46 or h_norm > 0.18:
            continue
        if w_norm * h_norm > 0.05:
            continue
        cx, cy = (x1 + x2) / 2 / width, (y1 + y2) / 2 / height
        in_region = False
        for rx1, ry1, rx2, ry2 in SEARCH_REGIONS:
            if rx1 <= cx <= rx2 and ry1 <= cy <= ry2:
                in_region = True
                break
        if not in_region:
            continue
        # 真正的水印通常贴着画面某一条边；画面内部的亮星/杂物不算
        near_left = x1 <= width * 0.06
        near_right = x2 >= width * 0.94
        near_top = y1 <= height * 0.06
        near_bottom = y2 >= height * 0.94
        if not ((near_left or near_right) and (near_top or near_bottom)):
            continue
        kept.append((x1, y1, x2, y2))
    return kept


def detect_watermark_boxes(path: str, is_image: bool, ocr_model="mobile"):
    """
    返回绝对像素坐标框列表 [(x1, y1, x2, y2), ...]。
    策略：OCR 找文字框 -> 过滤四角 -> 跨帧聚类 -> 合并相邻框。
    """
    # Windows 上必须先加载 torch，再加载 Paddle，否则 Paddle 之后加载的
    # Anaconda 版 torch 可能出现 shm.dll 依赖错误。
    import torch  # noqa: F401

    frames, width, height, _ = _load_sample_frames(path, is_image)
    if not frames:
        return []

    _load_backend_config(ocr_model=ocr_model)
    detector = _get_ocr_detector()

    per_frame = []
    for frame in frames:
        try:
            boxes = _ocr_text_boxes(detector, frame)
        except Exception as exc:
            log(f"[警告] OCR 检测失败：{exc}")
            boxes = []
        per_frame.append(_filter_corner_boxes(boxes, width, height))

    if not any(per_frame):
        return []

    # 跨帧聚类：同一位置反复出现才认为是固定水印
    clusters = []  # [{'items': [box...], 'frames': set()}]
    for frame_idx, boxes in enumerate(per_frame):
        for b in boxes:
            cx, cy = box_center(b)
            best = None
            best_score = None
            for c in clusters:
                ref = c["ref"]
                ref_cx, ref_cy = box_center(ref)
                dx = abs(cx - ref_cx) / width
                dy = abs(cy - ref_cy) / height
                overlap = iou(b, ref)
                if overlap >= 0.15 or (dx <= 0.025 and dy <= 0.02):
                    score = (1.0 - overlap) + dx + dy
                    if best_score is None or score < best_score:
                        best_score = score
                        best = c
            if best is None:
                clusters.append({"items": [b], "ref": b, "frames": {frame_idx}})
            else:
                best["items"].append(b)
                best["ref"] = union_box(best["items"])
                best["frames"].add(frame_idx)

    n_frames = len(frames)
    min_frames = max(1, math.ceil(n_frames * 0.5)) if n_frames > 2 else 1
    stable = [c for c in clusters if len(c["frames"]) >= min_frames]
    if not stable:
        # 视频很短或 OCR 不稳定时放宽一点
        min_frames = max(1, math.ceil(n_frames * 0.3))
        stable = [c for c in clusters if len(c["frames"]) >= min_frames]
    if not stable:
        return []

    boxes = [union_box(c["items"]) for c in stable]
    # 绝大多数平台水印在底部；如果有底部候选，就忽略顶部候选，
    # 避免把画面里固定出现的亮星/装饰误当成水印。
    bottom_boxes = [b for b in boxes if (b[1] + b[3]) / 2 >= height * 0.5]
    if bottom_boxes:
        boxes = bottom_boxes
    boxes = [expand_corner_box(b, width, height) for b in boxes]
    boxes = merge_nearby_boxes(boxes, width, height)

    # 去掉过大的框（可能是字幕/画面文字而不是水印）
    filtered = []
    for b in boxes:
        bw, bh = b[2] - b[0], b[3] - b[1]
        if bw / width > 0.55 or bh / height > 0.25:
            continue
        filtered.append(b)
    return filtered


# ---------------------------------------------------------------------------
# 掩码 / 模型
# ---------------------------------------------------------------------------

def make_mask(height: int, width: int, boxes):
    mask = np.zeros((height, width), dtype=np.uint8)
    for x1, y1, x2, y2 in boxes:
        cv2.rectangle(mask, (int(x1), int(y1)), (int(x2), int(y2)), 255, thickness=-1)
    return mask


def _ensure_lama_model():
    from backend.tools.model_config import ModelConfig
    from backend.tools.common_tools import merge_big_file_if_not_exists

    mc = ModelConfig()
    model_dir = Path(mc.LAMA_MODEL_DIR)
    model_path = model_dir / "big-lama.pt"
    if not model_path.exists():
        merge_big_file_if_not_exists(str(model_dir), "big-lama.pt")
    if not model_path.exists():
        raise RuntimeError(f"LAMA 模型不存在：{model_path}")
    return str(model_path)


def _get_device():
    import torch
    from backend.tools.hardware_accelerator import HardwareAccelerator

    acc = HardwareAccelerator.instance()
    if acc.has_cuda():
        return torch.device("cuda:0")
    return torch.device("cpu")


def _load_lama():
    from backend.inpaint.lama_inpaint import LamaInpaint

    device = _get_device()
    return LamaInpaint(device, _ensure_lama_model())


def _merge_regions(boxes, width, height, image_area_ratio=0.55):
    """按区域合并过近的框；返回用于 LAMA 的裁剪区域及局部掩码。"""
    boxes = [tuple(map(int, b)) for b in boxes]
    regions = []
    used = [False] * len(boxes)
    for i, b in enumerate(boxes):
        if used[i]:
            continue
        group = [b]
        used[i] = True
        changed = True
        while changed:
            changed = False
            g = union_box(group)
            for j, other in enumerate(boxes):
                if used[j]:
                    continue
                u = union_box([g, other])
                uw, uh = u[2] - u[0], u[3] - u[1]
                if uw * uh <= width * height * image_area_ratio * 0.5 and (
                        iou(g, other) > 0 or
                        (max(0, max(g[0], other[0]) - min(g[2], other[2])) <= width * 0.08 and
                         max(0, max(g[1], other[1]) - min(g[3], other[3])) <= height * 0.08)):
                    group.append(other)
                    used[j] = True
                    changed = True
        regions.append(union_box(group))

    result = []
    for b in regions:
        x1, y1, x2, y2 = b
        pad_x = max(16, int((x2 - x1) * 0.75))
        pad_y = max(16, int((y2 - y1) * 0.75))
        rx1, ry1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
        rx2, ry2 = min(width, x2 + pad_x), min(height, y2 + pad_y)
        # 贴边水印向外扩展时，允许把边留足
        if rx2 - rx1 < 32 or ry2 - ry1 < 32:
            continue
        mask = np.zeros((ry2 - ry1, rx2 - rx1), dtype=np.uint8)
        cv2.rectangle(mask, (x1 - rx1, y1 - ry1), (x2 - rx1, y2 - ry1), 255, -1)
        result.append({"roi": (rx1, ry1, rx2, ry2), "mask": mask})
    return result


# ---------------------------------------------------------------------------
# 图片 / 视频处理
# ---------------------------------------------------------------------------

def _write_image(path: Path, image: np.ndarray):
    ext = path.suffix.lower()
    params = []
    if ext in (".jpg", ".jpeg"):
        params = [cv2.IMWRITE_JPEG_QUALITY, 95]
    elif ext == ".png":
        params = [cv2.IMWRITE_PNG_COMPRESSION, 3]
    ok, buf = cv2.imencode(ext, image, params)
    if not ok:
        raise RuntimeError(f"图片编码失败：{path}")
    buf.tofile(str(path))


def remove_image_watermark(input_path: Path, output_path: Path, boxes):
    image = cv2.imdecode(np.fromfile(str(input_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"无法读取图片：{input_path}")
    h, w = image.shape[:2]
    regions = _merge_regions(boxes, w, h)
    if not regions:
        raise RuntimeError("没有有效的水印区域")

    progress(20, "加载 LAMA 模型")
    lama = _load_lama()
    progress(30, "重绘中", total=len(regions))
    for idx, region in enumerate(tqdm(regions, desc="LAMA 重绘", unit="区"), 1):
        rx1, ry1, rx2, ry2 = region["roi"]
        mask = region["mask"]
        crop = image[ry1:ry2, rx1:rx2].copy()
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        comp_rgb = lama.inpaint(rgb, mask)
        comp_bgr = cv2.cvtColor(comp_rgb, cv2.COLOR_RGB2BGR)
        sel = mask > 0
        crop[sel] = comp_bgr[sel]
        image[ry1:ry2, rx1:rx2] = crop
        progress(30 + 60 * idx / len(regions), f"重绘 {idx}/{len(regions)}",
                 done=idx, total=len(regions))

    progress(95, "写入图片")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_image(output_path, image)
    log(f"[完成] 图片已输出：{output_path}")


def _ffmpeg_merge_audio(video_no_audio: Path, original: Path, output: Path):
    from backend.tools.ffmpeg_cli import FFmpegCLI

    ffmpeg = FFmpegCLI.instance().ffmpeg_path
    cmd = [
        ffmpeg, "-y",
        "-i", str(video_no_audio),
        "-i", str(original),
        "-map", "0:v:0",
        "-map", "1:a:0?",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-loglevel", "error",
        str(output),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except Exception as exc:
        log(f"[警告] 音轨合并失败，直接保留无音轨视频：{exc}")
        if output.exists():
            output.unlink()
        shutil.copy2(video_no_audio, output)


def _has_audio_stream(path: Path) -> bool:
    try:
        import av
        with av.open(str(path)) as container:
            return len(container.streams.audio) > 0
    except Exception:
        # 判断失败时交给上游逻辑处理
        return True


def _read_video_info(path: Path):
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频：{path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    return fps, width, height, total


def remove_video_sttn(input_path: Path, output_path: Path, boxes):
    """使用仓库自带的 STTN 自动模式，按给定绝对框直接重绘（不做 OCR 检测）。"""
    from backend.main import SubtitleRemover
    from backend.tools.constant import InpaintMode
    from backend.config import config

    config.inpaintMode.value = InpaintMode.STTN_AUTO
    progress(20, "加载 STTN 模型")
    sr = SubtitleRemover(str(input_path))
    sr.sub_areas = [(y1, y2, x1, x2) for (x1, y1, x2, y2) in boxes]
    sr.video_out_path = str(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    total_frames = sr.frame_count

    def _on_progress(pct, _finished):
        progress(20 + 75 * max(0.0, min(100.0, pct)) / 100.0,
                 "STTN 重绘", done=int(total_frames * pct / 100.0), total=total_frames)
    sr.add_progress_listener(_on_progress)

    if not _has_audio_stream(input_path):
        # 原视频没有音轨：跳过上游的“提取音轨再合并”，直接把临时视频拷过去
        def _copy_without_audio():
            shutil.copy2(sr.video_temp_file.name, sr.video_out_path)
        sr.merge_audio_to_video = _copy_without_audio

    log(f"[STTN] 处理区域：{[(int(x1), int(y1), int(x2), int(y2)) for x1, y1, x2, y2 in boxes]}")
    sr.run()
    progress(97, "合并音轨")


def remove_video_lama(input_path: Path, output_path: Path, boxes,
                      preview_cb=None, ab_sections=None):
    """LAMA 逐帧重绘。只替换掩码内的像素，保留画面其余部分。

    preview_cb(original, result)：网页端实时对比预览，可选。
    ab_sections：只处理这些 frame range，其余帧原样写出。
    """
    from backend.tools.video_io import FramePrefetcher, FFmpegVideoWriter

    fps, width, height, total = _read_video_info(input_path)
    regions = _merge_regions(boxes, width, height)
    if not regions:
        raise RuntimeError("没有有效的水印区域")

    progress(20, "加载 LAMA 模型")
    lama = _load_lama()
    progress(25, "逐帧重绘", total=total)
    cap = cv2.VideoCapture(str(input_path))
    reader = FramePrefetcher(cap)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False, dir=str(output_path.parent))
    tmp_path = Path(tmp.name)
    tmp.close()

    writer = FFmpegVideoWriter(str(tmp_path), fps, (width, height))
    pbar = tqdm(total=total or None, unit="帧", desc="LAMA 逐帧")
    processed = 0
    frame_idx = -1
    last_pct = -10.0
    try:
        while True:
            ok, frame = reader.read()
            if not ok:
                break
            frame_idx += 1
            if ab_sections and not any(frame_idx in r for r in ab_sections):
                writer.write(frame)
                pbar.update(1)
                processed += 1
                if total:
                    pct = processed * 100.0 / total
                    if pct - last_pct >= 2.0 or processed == total:
                        progress(25 + 70 * pct / 100.0,
                                 f"LAMA 逐帧 {processed}/{total}",
                                 done=processed, total=total)
                        last_pct = pct
                continue
            original = frame.copy() if preview_cb else None
            for region in regions:
                rx1, ry1, rx2, ry2 = region["roi"]
                mask = region["mask"]
                crop = frame[ry1:ry2, rx1:rx2].copy()
                rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
                comp_rgb = lama.inpaint(rgb, mask)
                comp_bgr = cv2.cvtColor(comp_rgb, cv2.COLOR_RGB2BGR)
                sel = mask > 0
                crop[sel] = comp_bgr[sel]
                frame[ry1:ry2, rx1:rx2] = crop
            if preview_cb:
                preview_cb(original, frame.copy())
            writer.write(frame)
            pbar.update(1)
            processed += 1
            if total:
                pct = processed * 100.0 / total
                if pct - last_pct >= 2.0 or processed == total:
                    progress(25 + 70 * pct / 100.0,
                             f"LAMA 逐帧 {processed}/{total}",
                             done=processed, total=total)
                    last_pct = pct
    finally:
        pbar.close()
        reader.stop()
        writer.release()

    progress(97, "合并音轨")
    try:
        _ffmpeg_merge_audio(tmp_path, input_path, output_path)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    log(f"[完成] 视频已输出：{output_path}")


# ---------------------------------------------------------------------------
# 检测结果预览
# ---------------------------------------------------------------------------

def save_detection_preview(path: Path, is_image: bool, boxes, output_path: Path):
    if is_image:
        img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    else:
        cap = cv2.VideoCapture(str(path))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, total // 2))
        ok, img = cap.read()
        cap.release()
        if not ok or img is None:
            return None

    for idx, (x1, y1, x2, y2) in enumerate(boxes, 1):
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 2)
        cv2.putText(img, f"WM{idx}", (x1, max(18, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if ok:
        buf.tofile(str(output_path))
        return output_path
    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def resolve_boxes(path: Path, is_image: bool, args):
    """返回绝对像素框列表。"""
    if is_image:
        img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"无法读取图片：{path}")
        height, width = img.shape[:2]
    else:
        _, width, height, _ = _read_video_info(path)

    if args.box:
        if args.pixel_box:
            return [tuple(int(v) for v in args.box)]
        return [rel_box_to_abs(args.box, width, height)]

    preset = args.preset
    if preset == "auto":
        log(f"[检测] 正在自动查找水印位置：{path.name}")
        boxes = detect_watermark_boxes(str(path), is_image, ocr_model=args.ocr_model)
        if boxes:
            for i, b in enumerate(boxes, 1):
                log(f"       水印 {i}: 像素 {b}  相对 {format_box(abs_box_to_rel(b, width, height))}")
            return boxes
        log("[检测] 未找到稳定的文字水印，回退到右下角通用区域。")
        log("       如果水印在别处，请用 --box x1 y1 x2 y2 指定。")
        preset = "generic-br"

    if preset not in PRESETS:
        raise SystemExit(f"未知预设：{preset}。可用：auto, {', '.join(PRESETS)}")
    box = rel_box_to_abs(PRESETS[preset], width, height)
    log(f"[预设] {preset}: 像素 {box}  相对 {format_box(PRESETS[preset])}")
    return [box]


def gather_inputs(input_path: Path, recursive: bool):
    if input_path.is_file():
        return [input_path]
    if not input_path.is_dir():
        raise SystemExit(f"输入不存在：{input_path}")
    iterator = input_path.rglob("*") if recursive else input_path.glob("*")
    files = [p for p in iterator if p.is_file() and p.suffix.lower() in (VIDEO_EXTS | IMAGE_EXTS)]
    return sorted(files)


def output_for(input_path: Path, out_arg: Path | None, batch: bool, is_image: bool):
    if out_arg is None:
        return input_path.with_name(f"{input_path.stem}_no_wm{input_path.suffix}")
    looks_like_dir = out_arg.is_dir() or (not out_arg.exists() and out_arg.suffix == "")
    if batch or looks_like_dir:
        suffix = input_path.suffix.lower() or (".png" if is_image else ".mp4")
        return out_arg / f"{input_path.stem}_no_wm{suffix}"
    return out_arg


def unique_path(path: Path, overwrite: bool):
    if overwrite or not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 2
    while True:
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def process_one(input_path: Path, output_path: Path, args):
    is_image = input_path.suffix.lower() in IMAGE_EXTS
    progress(2, "准备")
    if args.preset == "auto":
        progress(6, "检测水印位置")
    boxes = resolve_boxes(input_path, is_image, args)
    progress(15, f"已确定 {len(boxes)} 处水印区域", boxes=[list(b) for b in boxes])

    if args.detect_only:
        if args.preview_out:
            preview = Path(args.preview_out)
        else:
            preview = output_path.with_name(f"{input_path.stem}_wm_detect.jpg")
        saved = save_detection_preview(input_path, is_image, boxes, preview)
        if saved:
            log(f"[预览] 检测框已画在：{saved}")
        progress(100, "检测完成", preview=str(saved) if saved else None,
                 boxes=[list(b) for b in boxes])
        return

    if is_image:
        remove_image_watermark(input_path, output_path, boxes)
    elif args.engine == "lama":
        remove_video_lama(input_path, output_path, boxes)
    else:
        remove_video_sttn(input_path, output_path, boxes)
    progress(100, "完成", output=str(output_path))


def parse_args():
    parser = argparse.ArgumentParser(
        description="AI 生成水印去除（豆包 / LibLib / 万相 / 即梦 / 可灵 / 海螺等）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-i", "--input", required=True, help="输入视频/图片，或文件夹")
    parser.add_argument("-o", "--output", default=None, help="输出文件或输出文件夹（可选）")
    parser.add_argument("--preset", default="auto",
                        help="水印预设：auto（自动检测）或 " + ", ".join(PRESETS))
    parser.add_argument("--box", nargs=4, type=float, metavar=("X1", "Y1", "X2", "Y2"),
                        help="手动指定水印框；默认是 0~1 相对坐标")
    parser.add_argument("--pixel-box", action="store_true", help="--box 使用绝对像素坐标")
    parser.add_argument("--engine", choices=["sttn", "lama"], default="sttn",
                        help="视频重绘引擎：sttn（默认，速度快、时序稳定）或 lama（细节更好、逐帧）")
    parser.add_argument("--ocr-model", choices=["mobile", "server"], default="mobile",
                        help="自动检测用的 OCR 模型：mobile（快）或 server（更准）")
    parser.add_argument("--detect-only", action="store_true", help="只检测并输出预览图，不执行去除")
    parser.add_argument("--preview-out", default=None,
                        help="--detect-only 时预览图的输出路径（网页控制台用）")
    parser.add_argument("--json-progress", action="store_true",
                        help="输出 @@WM_PROGRESS 机器可读进度行（网页控制台用）")
    parser.add_argument("--recursive", action="store_true", help="递归处理子文件夹")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出文件")
    return parser.parse_args()


def main():
    global _PROGRESS_ENABLED, _PROGRESS_BASE, _PROGRESS_SPAN
    args = parse_args()
    _PROGRESS_ENABLED = bool(args.json_progress)

    # 在切换 cwd 前把输入输出解析成绝对路径
    input_path = Path(args.input).expanduser().resolve()
    out_arg = Path(args.output).expanduser().resolve() if args.output else None
    args.preview_out = (
        str(Path(args.preview_out).expanduser().resolve()) if args.preview_out else None
    )

    # backend.config 的 qconfig 使用相对路径 config/config.json
    os.chdir(VSR_ROOT)

    files = gather_inputs(input_path, args.recursive)
    if not files:
        raise SystemExit(f"没有找到可处理的视频/图片：{input_path}")

    batch = input_path.is_dir() or len(files) > 1
    if batch and out_arg is not None and out_arg.exists() and out_arg.is_file():
        raise SystemExit("批量处理时 -o 必须是文件夹")
    if batch and out_arg is None:
        out_arg = input_path if input_path.is_dir() else input_path.parent
        out_arg = out_arg / "no_wm"

    # 避免把上一次的输出目录当成新的输入，导致 no_wm/no_wm 套娃
    if batch and out_arg is not None:
        if out_arg == input_path:
            raise SystemExit("批量输出目录不能和输入目录相同，请换一个 -o 目录")
        files = [f for f in files if out_arg not in f.parents]

    log(f"共 {len(files)} 个文件待处理。")
    if not files:
        raise SystemExit("排除输出目录后没有待处理文件。")
    failures = []
    start = time.time()
    for idx, file_path in enumerate(files, 1):
        is_image = file_path.suffix.lower() in IMAGE_EXTS
        _PROGRESS_BASE = (idx - 1) * 100.0 / len(files)
        _PROGRESS_SPAN = 100.0 / len(files)
        try:
            output_path = output_for(file_path, out_arg, batch, is_image)
            output_path = unique_path(output_path, args.overwrite)
            log(f"\n[{idx}/{len(files)}] {file_path.name} -> {output_path.name}")
            process_one(file_path, output_path, args)
        except Exception as exc:
            log(f"[失败] {file_path}: {exc}")
            failures.append((file_path, str(exc)))

    elapsed = time.time() - start
    log(f"\n处理结束，用时 {elapsed:.1f} 秒。成功 {len(files) - len(failures)} 个，失败 {len(failures)} 个。")
    if failures:
        for path, err in failures:
            log(f"  - {path.name}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
