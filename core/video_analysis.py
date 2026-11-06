"""视频分析：抽取代表帧，调用用户配置的视觉模型给出结构化诊断。"""

from __future__ import annotations

import json
import math
import re
import shutil
import statistics
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from . import prompt_opt

MAX_VIDEO_BYTES = 500 * 1024 * 1024
MAX_FRAME_COUNT = 40
FRAME_EDGE = 768
METRIC_EDGE = 64
MAX_AUDIO_METRIC_SECONDS = 120


class VideoAnalysisError(RuntimeError):
    """分析前置处理或模型输出无效。message 可直接展示给用户。"""


class VideoBaseline(BaseModel):
    avg_views: int = Field(gt=0, le=1_000_000_000)
    retention_2s: float | None = Field(default=None, ge=0, le=100)
    completion_rate: float | None = Field(default=None, ge=0, le=100)
    like_rate: float | None = Field(default=None, ge=0, le=100)
    comment_rate: float | None = Field(default=None, ge=0, le=100)
    share_rate: float | None = Field(default=None, ge=0, le=100)
    follow_rate: float | None = Field(default=None, ge=0, le=100)


class Issue(BaseModel):
    time: str
    evidence: str
    problem: str
    reason: str
    fix: str


class Action(BaseModel):
    priority: int = Field(ge=1, le=5)
    title: str
    steps: str
    expected_effect: str


class TimelineEntry(BaseModel):
    time: str
    visual: str
    role: Literal["hook", "setup", "payoff", "reaction", "transition", "filler", "unclear"]
    decision: Literal["keep", "shorten", "cut", "review"]
    reason: str


class Forecast(BaseModel):
    low_multiplier: float = Field(ge=0.05, le=10)
    likely_multiplier: float = Field(ge=0.05, le=10)
    high_multiplier: float = Field(ge=0.05, le=10)
    confidence: str
    assumptions: str


class AnalysisResult(BaseModel):
    score: int = Field(ge=0, le=100)
    headline: str
    summary: str
    strengths: list[str] = Field(min_length=1, max_length=5)
    timeline: list[TimelineEntry] = Field(min_length=1, max_length=MAX_FRAME_COUNT)
    issues: list[Issue] = Field(max_length=8)
    actions: list[Action] = Field(min_length=1, max_length=5)
    revised_prompt: str
    forecast: Forecast


SYSTEM_PROMPT = """你是严谨、直接、能手把手指导创作者的抖音短视频分析教练。
你只能根据用户提供的代表帧、帧时间、视频技术信息和（如有）本机计算的每秒 RMS 响度数值分析，不得假装看到了未提供的画面、听到了声音、识别了台词/音乐或知道平台算法。
视频前 3 秒每 0.5 秒抽样，之后约每秒抽样，最多 40 张。按时间顺序逐张梳理镜头，描述可见内容，并判断它承担的职责：hook（开场钩子）、setup（铺垫）、payoff（兑现）、reaction（反应）、transition（转场）、filler（信息重复/空等）、unclear（看不清）。
如提供本机自动测量的疑似黑边数据，可以作为线索；暗色画面可能误报，必须结合对应帧核对。不得声称已测量主体占屏比例，也不能仅凭画面推断播放量低的确定原因。
每个画面都要给一个剪辑决策：keep（保留）、shorten（压短）、cut（建议删除）、review（需结合完整视频确认）。不能凭一个静帧假装看到了整段动作；标明需要完整镜头验证之处。
如提供每秒响度数值，可指出响度的相对变化及时间段；它不等于听感或音乐节拍，不能据此断言具体音效、台词或声画精确同步。
逐项诊断开头吸引力、信息密度/节奏、主体清晰度、画面连续性、字幕可读性（仅当代表帧中可见）和结尾引导。
issues 必须分清证据与推断：evidence 写可直接观察的帧时间、画面事实或本机测量数值；problem 写由证据支持的问题；reason 用可能/倾向表达观众影响，不把假设说成已证实原因；fix 给出可执行剪辑操作。建议具体到时间点和操作。不要保证涨粉或爆款。
预测仅是以近期账号数据为基线的主观情景估计，不是统计预测；给出保守/较可能/乐观三个相对倍率，区间要克制，并解释假设与低置信度。
confidence 只能是“极低”“低”“中”或“高”之一。
只返回符合要求的 JSON，不要 Markdown 代码围栏或 JSON 以外的文字。
JSON 结构：
{
  "score": 0到100的整数,
  "headline": "一句话结论",
  "summary": "整体判断，并明确画面抽样和仅有响度数值时的分析限制",
  "strengths": ["值得保留的具体优点"],
  "timeline": [{"time":"00:00.0","visual":"该时刻实际可见的画面","role":"hook|setup|payoff|reaction|transition|filler|unclear","decision":"keep|shorten|cut|review","reason":"镜头职责和判断依据；不推测静帧之间未观察到的动作"}],
  "issues": [{"time":"00:03","evidence":"可核验的画面观察或本机测量数值","problem":"由证据支持的问题","reason":"可能的观众影响，不把推断写成事实","fix":"可执行修改"}],
  "actions": [{"priority":1,"title":"优先修改项","steps":"按步骤说明怎么改","expected_effect":"希望改善的指标/观感"}],
  "revised_prompt": "可直接继续创作的中文提示词；不要编造原视频里没有的剧情，保留原片明显的主题和风格",
  "forecast": {
    "low_multiplier": 0.7,
    "likely_multiplier": 1.0,
    "high_multiplier": 1.4,
    "confidence": "低",
    "assumptions": "解释相对近期账号平均播放量的情景估计及主要不确定因素"
  }
}
倍率必须满足 0.05 <= low_multiplier <= likely_multiplier <= high_multiplier <= 10。"""


def _sample_times(duration: float) -> list[float]:
    if not math.isfinite(duration) or duration <= 0:
        raise VideoAnalysisError("无法读取视频时长，请检查视频文件是否损坏。")
    if duration <= 0.5:
        return [max(0, duration / 2)]
    if duration <= 3:
        times = [index * 0.5 for index in range(math.ceil(duration * 2))]
        return sorted(set([*times, max(0, duration - 0.05)]))

    times = [index * 0.5 for index in range(7)]
    times.extend(float(second) for second in range(4, int(duration)))
    end = max(0, duration - 0.05)
    if end - times[-1] > 0.05:
        times.append(end)

    if len(times) <= MAX_FRAME_COUNT:
        return times

    tail = times[7:]
    tail_count = MAX_FRAME_COUNT - 7
    selected = [
        tail[round(index * (len(tail) - 1) / (tail_count - 1))]
        for index in range(tail_count)
    ]
    return [*times[:7], *selected]


def _format_timestamp(timestamp: float) -> str:
    centiseconds = int(math.floor(timestamp * 100 + 0.5))
    minutes, remainder = divmod(centiseconds, 6000)
    return f"{minutes:02d}:{remainder // 100:02d}.{remainder % 100:02d}"


def _rate(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    numerator, separator, denominator = value.partition("/")
    try:
        return float(numerator) / float(denominator) if separator else float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _inspect_video(video_path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise VideoAnalysisError(
            "找不到 ffprobe，无法读取视频时长。请安装 ffmpeg 并确保 ffprobe 在 PATH 中后重启服务。"
        )
    command = [
        ffprobe, "-v", "error", "-print_format", "json", "-show_format",
        "-show_streams", str(video_path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=45,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VideoAnalysisError(f"读取视频信息失败：{exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-500:] if completed.stderr else "文件无法解码"
        raise VideoAnalysisError(f"读取视频信息失败：{detail}")
    try:
        metadata = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise VideoAnalysisError("ffprobe 返回了无效的视频信息。") from exc

    stream = next(
        (item for item in metadata.get("streams", []) if item.get("codec_type") == "video"),
        None,
    )
    if stream is None:
        raise VideoAnalysisError("文件里没有可分析的视频画面。")
    duration = _rate(str(metadata.get("format", {}).get("duration") or ""))
    if duration <= 0:
        duration = _rate(str(stream.get("duration") or ""))
    if duration <= 0:
        raise VideoAnalysisError("无法读取视频时长，请检查视频文件是否损坏。")
    return {
        "duration": duration,
        "width": int(stream.get("width") or 0) or None,
        "height": int(stream.get("height") or 0) or None,
        "fps": _rate(stream.get("avg_frame_rate") or stream.get("r_frame_rate")) or None,
        "has_audio": any(
            item.get("codec_type") == "audio"
            for item in metadata.get("streams", [])
        ),
    }


def _extract_audio_rms(
    video_path: Path, duration: float, work_dir: Path, *, has_audio: bool
) -> dict[str, Any]:
    caveat = (
        "只表示每秒整体 RMS 响度（dBFS），不识别声音内容、台词、音乐或节拍；"
        "编码噪声和声道混合会影响读数。"
    )
    if not has_audio:
        return {"status": "no_audio", "samples": [], "caveat": "未检测到音轨。"}

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoAnalysisError(
            "找不到 ffmpeg，无法计算每秒音频响度。请安装 ffmpeg 并确保它在 PATH 中后重启服务。"
        )
    measured_duration = min(duration, MAX_AUDIO_METRIC_SECONDS)
    output = work_dir / "audio-rms.txt"
    filter_graph = (
        "aresample=48000,asetnsamples=n=48000:p=0,"
        "astats=metadata=1:reset=1,"
        "ametadata=mode=print:file=audio-rms.txt"
    )
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", str(video_path), "-t", f"{measured_duration:.3f}",
        "-map", "0:a:0", "-vn", "-af", filter_graph, "-f", "null", "-",
    ]
    try:
        completed = subprocess.run(
            command, cwd=work_dir, capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VideoAnalysisError(f"计算每秒音频响度失败：{exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-500:] or "FFmpeg 未能解码音轨"
        raise VideoAnalysisError(
            f"计算每秒音频响度失败：{detail}。可取消勾选响度分析后重试。"
        )
    try:
        metadata = output.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise VideoAnalysisError(f"读取音频响度结果失败：{exc}") from exc
    frame_pattern = re.compile(
        r"frame:\d+\s+pts:\S+\s+pts_time:([^\s]+)(.*?)(?=frame:\d+\s+pts:|$)",
        re.DOTALL,
    )
    rms_pattern = re.compile(
        r"lavfi\.astats\.Overall\.RMS_level=(-?(?:\d+(?:\.\d*)?|\.\d+)|-inf)"
    )
    by_second: dict[int, float | None] = {}
    for frame in frame_pattern.finditer(metadata):
        try:
            second = math.floor(float(frame.group(1)))
        except ValueError:
            continue
        if second < 0 or second >= measured_duration:
            continue
        rms = rms_pattern.search(frame.group(2))
        if rms is None:
            continue
        level = float(rms.group(1)) if rms.group(1) != "-inf" else None
        by_second.setdefault(second, round(level, 1) if level is not None else None)

    seconds = math.ceil(measured_duration)
    if not by_second:
        raise VideoAnalysisError("FFmpeg 未返回可用的逐秒 RMS 响度数据。")
    samples = [
        {"second": second, "rms_dbfs": by_second.get(second)}
        for second in range(seconds)
    ]
    return {
        "status": "analyzed",
        "samples": samples,
        "sample_count": len(by_second),
        "truncated": duration > MAX_AUDIO_METRIC_SECONDS,
        "caveat": caveat,
    }


def _measure_dark_edges(pixels: bytes) -> dict[str, Any]:
    if len(pixels) != METRIC_EDGE * METRIC_EDGE:
        raise VideoAnalysisError("抽样画面的亮度数据大小不正确，无法完成黑边检测。")

    rows = [
        sum(pixels[row * METRIC_EDGE:(row + 1) * METRIC_EDGE]) / METRIC_EDGE
        for row in range(METRIC_EDGE)
    ]
    columns = [
        sum(pixels[column::METRIC_EDGE]) / METRIC_EDGE
        for column in range(METRIC_EDGE)
    ]

    def extent(values: list[float]) -> int:
        count = 0
        for value in values:
            if value > 20:
                break
            count += 1
        return count

    top, bottom = extent(rows), extent(list(reversed(rows)))
    left, right = extent(columns), extent(list(reversed(columns)))

    center_rows = rows[top:METRIC_EDGE - bottom] if top + bottom < METRIC_EDGE else []
    center_columns = columns[left:METRIC_EDGE - right] if left + right < METRIC_EDGE else []
    center_row_mean = sum(center_rows) / len(center_rows) if center_rows else 0
    center_column_mean = sum(center_columns) / len(center_columns) if center_columns else 0
    horizontal_pair = (
        top >= 2 and bottom >= 2 and abs(top - bottom) <= 2
        and center_row_mean > 36
    )
    vertical_pair = (
        left >= 2 and right >= 2 and abs(left - right) <= 2
        and center_column_mean > 36
    )
    orientation = (
        "top_bottom" if horizontal_pair and top + bottom >= left + right
        else "left_right" if vertical_pair
        else None
    )
    if orientation == "top_bottom":
        border_fraction = (top + bottom) / METRIC_EDGE
    elif orientation == "left_right":
        border_fraction = (left + right) / METRIC_EDGE
    else:
        border_fraction = 0.0

    return {
        "orientation": orientation,
        "border_area_percent": round(min(100.0, border_fraction * 100), 1),
    }


def _summarize_black_bands(
    frames: list[tuple[float, Path, dict[str, Any]]],
) -> dict[str, Any]:
    candidates = [
        (timestamp, metric)
        for timestamp, _, metric in frames
        if metric["orientation"] is not None
    ]
    orientations = [metric["orientation"] for _, metric in candidates]
    orientation = (
        max(("top_bottom", "left_right"), key=orientations.count)
        if orientations else None
    )
    consistent = [
        (timestamp, metric)
        for timestamp, metric in candidates
        if metric["orientation"] == orientation
    ]
    likely = len(consistent) >= max(3, math.ceil(len(frames) * 0.6))
    areas = sorted(metric["border_area_percent"] for _, metric in consistent)
    return {
        "status": "possible" if likely else "not_consistent",
        "orientation": orientation,
        "frame_count": len(consistent),
        "sample_count": len(frames),
        "median_area_percent": statistics.median(areas) if likely else 0.0,
        "timestamps": [
            _format_timestamp(timestamp) for timestamp, _ in consistent
        ],
        "caveat": "仅为抽样帧边缘暗带的自动筛查；暗色画面可能误报，不能替代人工核对。",
    }


def _extract_frames(
    video_path: Path, duration: float, work_dir: Path
) -> list[tuple[float, Path, dict[str, Any]]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoAnalysisError(
            "找不到 ffmpeg，无法抽取视频画面。请安装 ffmpeg 并确保它在 PATH 中后重启服务。"
        )

    frames: list[tuple[float, Path, dict[str, Any]]] = []
    for index, timestamp in enumerate(_sample_times(duration)):
        output = work_dir / f"frame-{index:02d}.jpg"
        filter_graph = (
            f"split=2[image][metric];"
            f"[image]scale={FRAME_EDGE}:{FRAME_EDGE}:"
            "force_original_aspect_ratio=decrease[scaled];"
            f"[metric]scale={METRIC_EDGE}:{METRIC_EDGE},format=gray[gray]"
        )
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{timestamp:.3f}", "-i", str(video_path),
            "-filter_complex", filter_graph,
            "-map", "[scaled]", "-frames:v", "1", "-q:v", "4", str(output),
            "-map", "[gray]", "-frames:v", "1", "-pix_fmt", "gray",
            "-f", "rawvideo", "pipe:1",
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VideoAnalysisError(f"抽取第 {index + 1} 张画面失败：{exc}") from exc
        if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[-500:]
            if not detail:
                detail = "没有生成画面文件"
            raise VideoAnalysisError(f"抽取第 {index + 1} 张画面失败：{detail}")
        frames.append((timestamp, output, _measure_dark_edges(completed.stdout)))
    return frames


def _parse_result(text: str) -> AnalysisResult:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1]
        if candidate.endswith("```"):
            candidate = candidate[:-3].strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise VideoAnalysisError(
            f"视觉模型返回的分析结果不是有效 JSON，无法安全展示。模型原文：{text[:600]}"
        ) from exc
    if isinstance(data, dict) and isinstance(data.get("actions"), list):
        for action in data["actions"]:
            if isinstance(action, dict) and isinstance(action.get("steps"), list):
                steps = action["steps"]
                if all(isinstance(step, str) for step in steps):
                    action["steps"] = "\n".join(steps)
    try:
        result = AnalysisResult.model_validate(data)
    except ValidationError as exc:
        raise VideoAnalysisError(
            f"视觉模型返回的分析字段不完整或格式错误：{exc.errors(include_input=False)[:6]}"
        ) from exc
    forecast = result.forecast
    if not forecast.low_multiplier <= forecast.likely_multiplier <= forecast.high_multiplier:
        raise VideoAnalysisError("视觉模型返回的播放量区间顺序不合法，请重新分析。")
    if forecast.confidence not in {"极低", "低", "中", "高"}:
        raise VideoAnalysisError("视觉模型返回了未知的预测置信度，请重新分析。")
    return result


def _rounded_views(value: float) -> int:
    step = 10 if value < 1_000 else 100 if value < 10_000 else 1_000
    return int(math.floor(value / step + 0.5) * step)


def analyze(
    video_path: Path,
    *,
    video_name: str,
    baseline: VideoBaseline,
    model: str,
    base_url: str,
    api_key: str,
    include_audio_metrics: bool = False,
    video_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对本地视频进行一次有明确输入边界的画面分析。"""
    video_path = video_path.resolve()
    if not video_path.is_file():
        raise VideoAnalysisError("视频文件不存在，请重新选择。")
    size = video_path.stat().st_size
    if size > MAX_VIDEO_BYTES:
        raise VideoAnalysisError(
            f"视频文件为 {size / 1024 / 1024:.1f}MB，超过分析上限 500MB。"
        )

    if video_path.suffix.lower() not in {".mp4", ".mov"}:
        raise VideoAnalysisError("请选择 MP4 或 MOV 视频文件。")
    inspected = video_info or _inspect_video(video_path)
    duration = inspected.get("duration")
    if not duration or duration <= 0:
        raise VideoAnalysisError("无法读取视频时长，请检查视频文件是否损坏。")
    if not model.strip():
        raise VideoAnalysisError("设置里的视觉模型名称为空，请先到「设置」填写模型名。")

    width = inspected.get("width")
    height = inspected.get("height")
    fps = inspected.get("fps")
    expected_frame_count = len(_sample_times(float(duration)))
    technical = (
        f"视频信息：时长 {duration} 秒，分辨率 {width or '未知'}×{height or '未知'}，"
        f"帧率 {fps or '未知'}fps，大小 {size / 1024 / 1024:.1f}MB。\n"
        f"共按时长抽取 {expected_frame_count} 帧：前 3 秒每 0.5 秒一张，之后约每秒一张（总数最多 40），"
        "仍不能代表所有快速剪切和动作细节。"
    )
    baseline_text = (
        f"账号近期平均播放量：{baseline.avg_views}\n"
        + "\n".join(
            f"{label}：{value}%"
            for label, value in (
                ("2秒留存率", baseline.retention_2s),
                ("完播率", baseline.completion_rate),
                ("点赞率", baseline.like_rate),
                ("评论率", baseline.comment_rate),
                ("分享率", baseline.share_rate),
                ("关注转化率", baseline.follow_rate),
            )
            if value is not None
        )
    )

    with tempfile.TemporaryDirectory(prefix="wan3-video-analysis-") as temp:
        work_dir = Path(temp)
        frames = _extract_frames(video_path, float(duration), work_dir)
        if include_audio_metrics:
            audio_metrics = _extract_audio_rms(
                video_path, float(duration), work_dir,
                has_audio=bool(inspected.get("has_audio")),
            )
        else:
            audio_metrics = {
                "status": "not_requested",
                "samples": [],
                "sample_count": 0,
                "caveat": "用户未启用音频响度分析。",
            }
        images: list[tuple[str, str]] = []
        for timestamp, frame_path, _ in frames:
            uri = prompt_opt.image_data_uri(frame_path)
            if uri is None:
                raise VideoAnalysisError(
                    f"无法读取抽取画面 {frame_path.name}，请确认本机磁盘可读写。"
                )
            images.append((_format_timestamp(timestamp), uri))

        user_text = (
            "请诊断这条抖音短视频，并给出可执行的下一版修改建议。\n\n"
            f"{technical}\n\n账号基线：\n{baseline_text}\n\n"
            f"以下是所有画面的精确时间戳：{'、'.join(label for label, _ in images)}。\n"
            "如黑边测量提示暗色边缘，只能说‘疑似黑边’，需要结合样本帧确认，不得断言是黑边。\n"
            "附图按时间顺序对应标注时间。timeline 必须按顺序为每张图各写一项，"
            "time 字段必须原样复制对应时间戳，不得漏图或增加项目；"
            "用相邻时间点描述变化；不要把抽帧误差伪装成精确剪辑边界。"
            "预测播放量只能表示相对账号近期平均表现的情景估计，"
            "不能承诺实际结果。"
        )
        black_bands = _summarize_black_bands(frames)
        if audio_metrics["status"] == "analyzed":
            audio_levels = ", ".join(
                f"{sample['second']}s="
                f"{sample['rms_dbfs'] if sample['rms_dbfs'] is not None else '低于测量范围'} dBFS"
                for sample in audio_metrics["samples"]
            )
            audio_note = (
                f"\n本机每秒 RMS 响度曲线（dBFS）：{audio_levels}。"
                "仅将这些数字发送给模型，音频文件未发送；这不表示听感、台词或音乐节拍。"
            )
            if audio_metrics["truncated"]:
                audio_note += f"视频超过 {MAX_AUDIO_METRIC_SECONDS} 秒，响度曲线只覆盖前 {MAX_AUDIO_METRIC_SECONDS} 秒。"
        elif audio_metrics["status"] == "no_audio":
            audio_note = "\n视频未检测到音轨，因此没有音频响度数据。"
        else:
            audio_note = "\n用户未启用音频响度分析；不得对声音作判断。"
        user_text += audio_note
        if black_bands["status"] == "possible":
            orientation = (
                "上下" if black_bands["orientation"] == "top_bottom" else "左右"
            )
            user_text += (
                f"\n本机边缘暗带筛查：在 {black_bands['frame_count']}/{black_bands['sample_count']} "
                f"张抽样帧中发现一致的疑似{orientation}黑边，"
                f"中位估计占画面面积 {black_bands['median_area_percent']}%，"
                f"时间点 {', '.join(black_bands['timestamps'])}。这是像素启发式线索，"
                "暗场景可能误报，必须以对应原图核对后再建议裁切。"
            )
        elif black_bands["frame_count"]:
            user_text += (
                f"\n本机边缘暗带筛查仅在 {black_bands['frame_count']}/"
                f"{black_bands['sample_count']} 张抽样帧发现不一致信号，"
                f"时间点 {', '.join(black_bands['timestamps'])}；样本量或一致性不足，"
                "不得断言为黑边，需核对原图。"
            )
        else:
            user_text += (
                "\n本机边缘暗带筛查未得到一致的黑边证据；"
                "这不代表所有帧都没有黑边，也不能用于判断主体占屏面积。"
            )
        text = prompt_opt.call_model(
            base_url, api_key, model, SYSTEM_PROMPT, user_text, images,
            operation="视频分析",
        )
        result = _parse_result(text)
        if len(result.timeline) != len(images):
            raise VideoAnalysisError(
                f"视觉模型只返回了 {len(result.timeline)} 条逐段拆解，"
                f"但本次抽取了 {len(images)} 张画面。请重试；若仍失败，可暂时分析较短的视频。"
            )
        for entry, (timestamp, _, _) in zip(result.timeline, frames):
            entry.time = _format_timestamp(timestamp)

    result_data = result.model_dump()
    avg_views = baseline.avg_views
    result_data["forecast"]["views"] = {
        "low": _rounded_views(avg_views * result.forecast.low_multiplier),
        "likely": _rounded_views(avg_views * result.forecast.likely_multiplier),
        "high": _rounded_views(avg_views * result.forecast.high_multiplier),
        "baseline": avg_views,
    }
    return {
        "video_name": video_name,
        "model": model,
        "video_info": {
            "duration": duration,
            "width": width,
            "height": height,
            "fps": fps,
            "sampled_frames": [label for label, _ in images],
            "black_bands": black_bands,
            "audio_metrics": audio_metrics,
        },
        "baseline": baseline.model_dump(),
        "analysis": result_data,
    }
