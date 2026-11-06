# -*- coding: utf-8 -*-
"""网页控制台专用的去水印执行器。

控制台把任务参数写成 JSON（--spec），这里用 video-subtitle-remover 原生的
SubtitleRemover 跑完整流程，因此算法、检测模型、STTN/ProPainter 参数、
A/B 区间、多选框等行为与桌面版一致。

和桌面版不同的只有输出方式：
    - 进度用 @@WM_PROGRESS {...} 打到 stdout
    - 处理中用 preview_cb 写一张左右对比图，网页可以轮询显示

这个文件不替代 wm_remove.py；wm_remove.py 仍然是命令行/桌面版用的简化入口。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
VSR_ROOT = Path(
    os.environ.get("WAN3_VSR_ROOT")
    or (HERE.parents[2] / "video-subtitle-remover")
)
ROOT = HERE
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(VSR_ROOT))

import wm_remove as wm  # noqa: E402

MODE_TO_INPAINT = {
    "sttn-auto": "STTN_AUTO",
    "sttn-det": "STTN_DET",
    "lama": "LAMA",
    "propainter": "PROPAINTER",
    "opencv": "OPENCV",
}

ADVANCED_CONFIG = {
    # 配置项名 -> 类型
    "sttnNeighborStride": int,
    "sttnReferenceLength": int,
    "sttnMaxLoadNum": int,
    "propainterMaxLoadNum": int,
    "subtitleAreaDeviationPixel": int,
    "subtitleYXAxisDifferencePixel": int,
    "subtitleAreaYAxisDifferencePixel": int,
    "subtitleAreaPixelToleranceXPixel": int,
    "subtitleAreaPixelToleranceYPixel": int,
    "subtitleTimelineBackwardFrameCount": int,
    "subtitleTimelineForwardFrameCount": int,
}

DEFAULT_ADVANCED = {
    "sttnNeighborStride": 5,
    "sttnReferenceLength": 10,
    "sttnMaxLoadNum": 50,
    "propainterMaxLoadNum": 24,
    "subtitleAreaDeviationPixel": 10,
    "subtitleYXAxisDifferencePixel": 10,
    "subtitleAreaYAxisDifferencePixel": 20,
    "subtitleAreaPixelToleranceXPixel": 20,
    "subtitleAreaPixelToleranceYPixel": 20,
    "subtitleTimelineBackwardFrameCount": 3,
    "subtitleTimelineForwardFrameCount": 3,
}


def log(*args) -> None:
    print(*args, flush=True)


def read_spec(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def media_info(path: str, is_image: bool):
    if is_image:
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"无法读取图片：{path}")
        h, w = img.shape[:2]
        return {"width": w, "height": h, "fps": 0.0, "frames": 1}
    fps, width, height, total = wm._read_video_info(Path(path))
    return {"width": width, "height": height, "fps": fps, "frames": total}


def set_advanced(settings: dict) -> None:
    from backend.config import config

    for name, caster in ADVANCED_CONFIG.items():
        if name not in settings:
            continue
        try:
            value = caster(settings[name])
        except (TypeError, ValueError):
            continue
        item = getattr(config, name, None)
        if item is not None:
            item.value = value


def make_preview_callback(path: str, boxes_px):
    """写左右对比图；调用频率限制在 2 次/秒，避免 IO 拖慢推理。"""
    state = {"last": 0.0, "warned": False}

    def callback(original, result):
        now = time.time()
        if now - state["last"] < 0.5:
            return
        state["last"] = now
        try:
            left = original.copy()
            for x1, y1, x2, y2 in boxes_px:
                cv2.rectangle(left, (int(x1), int(y1)), (int(x2), int(y2)),
                              (0, 0, 255), 2)
            pair = cv2.hconcat([left, result])
            max_width = 1280
            if pair.shape[1] > max_width:
                scale = max_width / pair.shape[1]
                pair = cv2.resize(pair, (max_width, int(pair.shape[0] * scale)))
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp.jpg")
            # cv2.imwrite 在 Windows 上写中文路径会静默失败，必须用 imencode + tofile
            ok, buf = cv2.imencode(".jpg", pair, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if not ok:
                raise RuntimeError("JPEG 编码失败")
            buf.tofile(str(tmp))
            os.replace(tmp, target)
        except Exception as exc:  # noqa: BLE001 - 预览失败不能拖垮主任务
            if not state["warned"]:
                state["warned"] = True
                log(f"[预览] 写入失败（不影响处理）：{exc}")

    return callback


def run_vsr_mode(spec: dict, boxes_px, is_image: bool) -> None:
    """用 VSR 原生 SubtitleRemover 跑选定算法。"""
    from backend.config import config
    from backend.tools.constant import InpaintMode, SubtitleDetectMode
    from backend.main import SubtitleRemover
    from backend.tools.hardware_accelerator import HardwareAccelerator

    mode = spec["mode"]
    if is_image:
        mode = "lama"  # 桌面版图片模式固定 LAMA
    enum_name = MODE_TO_INPAINT.get(mode)
    if enum_name is None:
        raise RuntimeError(f"未知算法：{mode}")
    inpaint_mode = InpaintMode[enum_name]

    detect_model = spec.get("detect_model") or "fast"
    config.inpaintMode.value = inpaint_mode
    config.subtitleDetectMode.value = (
        SubtitleDetectMode.PP_OCRv5_SERVER if detect_model == "precise"
        else SubtitleDetectMode.PP_OCRv5_MOBILE
    )
    config.hardwareAcceleration.value = bool(spec.get("hardware_acceleration", True))
    set_advanced(spec.get("advanced") or {})

    acc = HardwareAccelerator.instance()
    acc.set_enabled(bool(spec.get("hardware_acceleration", True)))

    sr = SubtitleRemover(str(spec["input"]), gui_mode=True)
    sr.video_out_path = str(spec["output"])
    sr.sub_areas = [(y1, y2, x1, x2) for (x1, y1, x2, y2) in boxes_px]

    # A/B 区间（秒 -> frame range）
    ab = spec.get("ab")
    if ab and not is_image:
        start = int(round(float(ab[0]) * sr.fps))
        end = int(round(float(ab[1]) * sr.fps))
        start = max(0, min(start, max(0, sr.frame_count - 1)))
        end = max(start + 1, min(end, sr.frame_count))
        if end > start:
            sr.ab_sections = [range(start, end)]

    preview_path = spec.get("live_preview")
    if preview_path and not is_image:
        sr.update_preview_with_comp = make_preview_callback(preview_path, boxes_px)

    total = max(1, sr.frame_count)
    sr.add_progress_listener(
        lambda pct, _finished: wm.progress(
            15 + 80 * max(0.0, min(100.0, pct)) / 100.0,
            f"{mode} 处理中",
            done=int(total * pct / 100.0),
            total=total,
        )
    )
    sr.append_output = lambda *args: log(*args)
    log(f"[VSR] 算法={mode} 检测模型={detect_model} 区域数={len(boxes_px)}")

    # 文字检测阶段 SubtitleDetect 只更新 progress_total，不触发 listener；
    # 轮询一下，网页进度条不会一直停在 20%。
    stop_poll = threading.Event()
    ocr_mode = mode in ("sttn-det", "lama", "propainter", "opencv")

    if ocr_mode:
        wm.progress(22, "检测文字…")

    def poll_progress():
        last = -1.0
        while not stop_poll.wait(0.6):
            try:
                pct = float(getattr(sr, "progress_total", 0) or 0)
            except (TypeError, ValueError):
                continue
            if abs(pct - last) < 0.5:
                continue
            last = pct
            stage = "检测文字…" if (ocr_mode and pct < 50) else f"{mode} 处理中"
            wm.progress(15 + 80 * max(0.0, min(100.0, pct)) / 100.0,
                        stage, done=int(total * pct / 100.0), total=total)

    poller = threading.Thread(target=poll_progress, daemon=True)
    poller.start()
    try:
        sr.run()
    finally:
        stop_poll.set()


def run_direct_mode(spec: dict, boxes_px) -> None:
    """直接框选重绘：不跑 OCR，适合纯 logo / 已知精确位置。"""
    from backend.tools.hardware_accelerator import HardwareAccelerator

    HardwareAccelerator.instance().set_enabled(
        bool(spec.get("hardware_acceleration", True))
    )
    is_image = spec.get("media_type") == "image"
    live_preview = spec.get("live_preview")
    if is_image:
        wm.remove_image_watermark(
            Path(spec["input"]), Path(spec["output"]), boxes_px
        )
        return

    ab_sections = None
    ab = spec.get("ab")
    if ab:
        info = media_info(spec["input"], False)
        start = int(round(float(ab[0]) * info["fps"]))
        end = int(round(float(ab[1]) * info["fps"]))
        start = max(0, min(start, max(0, info["frames"] - 1)))
        end = max(start + 1, min(end, info["frames"]))
        if end > start:
            ab_sections = [range(start, end)]
    preview_cb = make_preview_callback(live_preview, boxes_px) if live_preview else None
    wm.remove_video_lama(
        Path(spec["input"]), Path(spec["output"]), boxes_px,
        preview_cb=preview_cb, ab_sections=ab_sections,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True, help="JSON 任务参数")
    args = parser.parse_args()
    spec = read_spec(args.spec)

    wm._PROGRESS_ENABLED = True
    wm._PROGRESS_BASE = 0.0
    wm._PROGRESS_SPAN = 100.0

    input_path = str(spec["input"])
    output_path = str(spec["output"])
    is_image = spec.get("media_type") == "image"
    mode = spec.get("mode", "sttn-auto")
    detect_only = bool(spec.get("detect_only"))

    wm.progress(3, "准备")
    info = media_info(input_path, is_image)
    width, height = info["width"], info["height"]

    # 相对坐标 -> 像素
    boxes_px = []
    for box in spec.get("areas") or []:
        try:
            boxes_px.append(wm.rel_box_to_abs(box, width, height))
        except (TypeError, ValueError):
            continue

    # 自动检测（OCR）
    if spec.get("auto_detect"):
        wm.progress(6, "检测水印位置")
        detect_model = spec.get("detect_model") or "fast"
        boxes_px = wm.detect_watermark_boxes(
            input_path, is_image,
            ocr_model="server" if detect_model == "precise" else "mobile",
        )
        if not boxes_px:
            raise RuntimeError("没有检测到稳定的文字水印。请手动框选后再试。")
        wm.progress(15, f"已找到 {len(boxes_px)} 处水印",
                    boxes=[list(b) for b in boxes_px],
                    width=width, height=height)
    else:
        if not boxes_px:
            if mode in ("sttn-auto", "lama-direct"):
                raise RuntimeError("请先在画面上框出水印区域，再开始处理。")
            boxes_px = [(0, 0, width, height)]
        wm.progress(15, f"已确定 {len(boxes_px)} 处区域",
                    boxes=[list(b) for b in boxes_px],
                    width=width, height=height)

    if detect_only:
        preview = spec.get("live_preview") or spec.get("preview")
        saved = wm.save_detection_preview(input_path, is_image, boxes_px, Path(preview))
        if saved:
            log(f"[预览] 检测框已画在：{saved}")
        wm.progress(100, "检测完成", boxes=[list(b) for b in boxes_px],
                    preview=str(saved) if saved else None)
        return 0

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if mode == "lama-direct":
        wm.progress(20, "加载 LAMA 模型")
        run_direct_mode(spec, boxes_px)
    else:
        wm.progress(20, "加载模型")
        run_vsr_mode(spec, boxes_px, is_image)

    if not Path(output_path).exists():
        raise RuntimeError(f"处理结束但没有生成输出文件：{output_path}")

    wm.progress(100, "完成", output=output_path,
                boxes=[list(b) for b in boxes_px])
    log(f"[完成] {output_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        print(f"[失败] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
