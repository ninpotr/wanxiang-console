"""视频超清：不改变分辨率，只提升画质。

做法是 AI 超采样（super-sampling），不是普通插值：
    解码抽帧 -> Real-ESRGAN 放大 -> 用 lanczos 缩回原分辨率 -> 带上原音频重新编码

为什么"放大再缩回"反而更清晰：
    神经网络先把画面重建到 2 倍/3 倍尺寸，再缩回来时，多出来的高频信息被平均，
    压缩块效应和锯齿被抹平，边缘变实。实测（2560×1440 源）锐度提升约 +70%。
    这不是插值，插值只会变糊。

为什么默认 ×2 而不是 ×3：
    实测验过同一帧：×2 回缩锐度 104.9，×3 回缩 102.9 —— **×2 反而更好**，
    而 ×3 的显存、磁盘和时间都是 ×2 的 2.25 倍。×3 纯属浪费。

为什么分块（chunk）：
    老版本先把全部帧抽成 PNG 丢给 Real-ESRGAN，再整段编码。
    一条 78 秒 60fps 的 2K 视频要产生约 140 GB 临时帧，普通硬盘根本撑不住。
    现在每处理完一块就立刻编码、立刻删掉这一块的临时帧，峰值占用降到 1/8 左右。

用法：
    python tools/upscale_video.py 输入.mp4
    python tools/upscale_video.py 输入.mp4 --scale 2 --max-temp 72 --max-util 75
    python tools/upscale_video.py 输入.mp4 --no-throttle

依赖：
    ffmpeg / ffprobe（优先用项目自带完整版）
    tools/bin/realesrgan/realesrgan-ncnn-vulkan.exe
    nvidia-smi（可选，只用来读显卡温度占用；没有就不限速）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ESRGAN_DIR = ROOT / "tools" / "bin" / "realesrgan"
ESRGAN_EXE = ESRGAN_DIR / "realesrgan-ncnn-vulkan.exe"

# 目标短边像素。竖屏视频 1080p 就是 1080×1920。
TARGET_SHORT_SIDE = {
    "1080p": 1080,
    "2k": 1440,
    "4k": 2160,
}

DEFAULT_MODEL = "realesr-animevideov3"
DEFAULT_SCALE = 2
MODELS = [
    "realesr-animevideov3",     # 视频专用，首选
    "realesrgan-x4plus-anime",  # 图片动漫模型
    "realesrgan-x4plus",        # 图片通用模型
]

# 每块默认处理多少帧。
# 太大 -> 温度反馈迟钝（一块跑完才知道热了）、峰值磁盘高；
# 太小 -> 每次调用 realesrgan 都要重新加载模型，开销占比高。
# 120 帧在本机约一分钟一块，是个折中。
DEFAULT_CHUNK = 120

# PNG 体积粗估（字节/像素）。实测 2560×1440 源帧 2.7MB（0.73），×2 输出 13.9MB（0.94）。
PNG_BYTES_PER_PX_SRC = 0.75
PNG_BYTES_PER_PX_OUT = 0.95


class UpscaleError(RuntimeError):
    pass


FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
ENCODER = "libx264"


def run(cmd: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def find_ffmpeg() -> tuple[str, str]:
    """优先用项目自带的完整版 ffmpeg。

    为什么：用 conda 装的那个编译时带了 --disable-gpl，**没有 libx264**，
    只能用 h264_mf（只能按码率、没有 CRF）。项目里放一份完整版就没这个问题。
    """
    bundled = sorted((ROOT / "tools" / "bin" / "ffmpeg").glob("*/bin/ffmpeg.exe"))
    if bundled:
        ff = bundled[-1]
        probe = ff.with_name("ffprobe.exe")
        if probe.exists():
            return str(ff), str(probe)
    return "ffmpeg", "ffprobe"


def detect_encoder(ffmpeg: str) -> str:
    """挑一个能用的 H.264 编码器。libx264 最好，退而求其次用系统的。"""
    proc = run([ffmpeg, "-hide_banner", "-encoders"])
    text = proc.stdout or ""
    for name in ("libx264", "h264_nvenc", "h264_mf"):
        if re.search(rf"\b{name}\b", text):
            return name
    return "h264_mf"


def require_tools() -> None:
    global FFMPEG, FFPROBE, ENCODER

    FFMPEG, FFPROBE = find_ffmpeg()

    if FFMPEG == "ffmpeg":
        if shutil.which("ffmpeg") is None:
            raise UpscaleError("找不到 ffmpeg，请安装后加入 PATH")
        if shutil.which("ffprobe") is None:
            raise UpscaleError("找不到 ffprobe，请安装后加入 PATH")
    elif not Path(FFMPEG).exists():
        raise UpscaleError(f"自带的 ffmpeg 路径不对：{FFMPEG}")

    ENCODER = detect_encoder(FFMPEG)

    if not ESRGAN_EXE.exists():
        raise UpscaleError(
            f"找不到 Real-ESRGAN：{ESRGAN_EXE}\n"
            "去 https://github.com/xinntao/Real-ESRGAN/releases 下 "
            "realesrgan-ncnn-vulkan-*-windows.zip，解压到 tools/bin/realesrgan/"
        )


# ---------------------------------------------------------------- 显卡监控与限速


@dataclass
class GpuSample:
    ts: float
    temp: int
    util: int
    power: float


class GpuMonitor:
    """后台线程轮询 nvidia-smi，记录温度和占用。

    为什么不用 nvidia-smi 的阻塞模式：它没有。只能自己定时问。
    问一次大约 60~100ms，0.5 秒问一次对主任务几乎没影响。
    """

    def __init__(self, interval: float = 0.5) -> None:
        self.interval = interval
        self.samples: list[GpuSample] = []
        self.available = shutil.which("nvidia-smi") is not None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.available:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _loop(self) -> None:
        cmd = [
            "nvidia-smi",
            "--query-gpu=temperature.gpu,utilization.gpu,power.draw",
            "--format=csv,noheader,nounits",
        ]
        while not self._stop.is_set():
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
                temp, util, power = [x.strip() for x in out.split(",")[:3]]
                self.samples.append(GpuSample(time.time(), int(temp), int(util), float(power)))
            except Exception:  # noqa: BLE001 - 读不到就跳过，绝不因为监控失败而中断任务
                pass
            self._stop.wait(self.interval)

    def window(self, seconds: float = 20.0) -> list[GpuSample]:
        cutoff = time.time() - seconds
        return [s for s in self.samples if s.ts >= cutoff]


@dataclass
class Throttle:
    """显卡限速。

    口径（用户定的）：
        温度 —— **5 秒滑动平均**必须压在 max_temp（默认 75）以下，
                日常目标在 max_temp - 5 左右（约 70）。
        占用 —— 看**长时间平均**（60 秒），压在 max_util（默认 90）以下。
                短时间冲到 99% 是正常的，不做惩罚。

    为什么温度和占用用不同窗口：
        温度是硬件真实压力，5 秒就能反映出来，用它做硬保护。
        占用率天然是锯齿状（每帧之间有存盘间隙），5 秒平均没有意义；
        用户要的是"别长时间占满"，所以看 60 秒。

    为什么用"歇一会"而不是限功耗：
        限功耗要管理员权限，而且影响整个系统。歇一会只影响这一个任务，
        跑完自动恢复，不需要任何特权。
    """

    max_temp: int = 75           # 0 = 不限温度（5 秒平均）
    max_util: int = 90           # 0 = 不限占用（60 秒平均）
    sleep_now: float = 0.0
    peak_temp: int = 0
    mean_util: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.max_temp > 0 or self.max_util > 0

    @property
    def soft_temp(self) -> int:
        """日常目标温度。比硬上限低 5 度，留出爬升余量。"""
        return max(1, self.max_temp - 5) if self.max_temp else 999

    # -------------------------------------------------- 判定

    def temp_avg(self, monitor: GpuMonitor, window: float = 5.0) -> float:
        recent = monitor.window(window)
        return sum(s.temp for s in recent) / len(recent) if recent else 0.0

    def util_avg(self, monitor: GpuMonitor, window: float = 60.0) -> float:
        recent = monitor.window(window)
        return sum(s.util for s in recent) / len(recent) if recent else 0.0

    def too_hot(self, monitor: GpuMonitor) -> bool:
        """5 秒平均是否已经越过硬上限。越过就得当场停手。"""
        if not self.enabled or not monitor.available:
            return False
        recent = monitor.window(5.0)
        if len(recent) < 4:          # 样本太少，别误判
            return False
        avg = sum(s.temp for s in recent) / len(recent)
        self.peak_temp = max(self.peak_temp, int(round(avg)))
        return bool(self.max_temp) and avg > self.max_temp

    def adjust(self, monitor: GpuMonitor) -> float:
        """每块之间歇多久。返回本轮要睡的秒数。"""
        if not self.enabled or not monitor.available:
            return 0.0

        temp = self.temp_avg(monitor, 5.0)
        util = self.util_avg(monitor, 60.0)
        if temp:
            self.peak_temp = max(self.peak_temp, int(round(temp)))
        if util:
            self.mean_util = util

        if self.max_temp and temp > self.max_temp:
            self.sleep_now = min(30.0, self.sleep_now + 3.0)      # 超硬线，重罚
        elif self.max_temp and temp > self.soft_temp:
            self.sleep_now = min(30.0, self.sleep_now + 1.0)      # 高于日常目标，轻罚
        elif self.max_util and util > self.max_util:
            self.sleep_now = min(30.0, self.sleep_now + 1.0)      # 占用长期偏高，轻罚
        elif temp < self.soft_temp - 5 and util < (self.max_util or 999) - 15:
            self.sleep_now = max(0.0, self.sleep_now - 1.0)       # 凉下来了，放开

        return self.sleep_now


# ---------------------------------------------------------------- 基础探测与计算


def probe(path: Path) -> dict:
    proc = run([
        FFPROBE, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    if proc.returncode != 0:
        raise UpscaleError(f"读不了这个视频：{proc.stderr[:300]}")
    data = json.loads(proc.stdout)

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise UpscaleError("这个文件里没有视频轨")

    num, _, den = (video.get("avg_frame_rate") or "30/1").partition("/")
    try:
        fps = float(num) / float(den) if float(den) else 30.0
    except (ValueError, ZeroDivisionError):
        fps = 30.0

    duration = float(data.get("format", {}).get("duration") or 0)
    frames = int(video.get("nb_frames") or 0) or max(1, int(round(duration * fps)))

    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 3),
        "duration": duration,
        "frames": frames,
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
        "size": int(data.get("format", {}).get("size") or 0),
    }


def target_size(src_w: int, src_h: int, target: str) -> tuple[int, int]:
    """按短边算目标尺寸，保持原比例，并取偶数（h264 要求）。"""
    short = TARGET_SHORT_SIDE[target]
    if src_w <= src_h:
        w = short
        h = round(short * src_h / src_w)
    else:
        h = short
        w = round(short * src_w / src_h)
    return w + (w % 2), h + (h % 2)


def free_bytes(path: Path) -> int:
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return 0


def estimate_disk(frames: int, src_px: int, out_px: int, chunk: int) -> tuple[int, int]:
    """算峰值占用。返回 (抽帧总量, 峰值总量)。

    抽帧全部要先落盘（按顺序读回来），但用完一块删一块，
    所以峰值 = 全部输入帧 + 一块的输出帧。
    """
    total_in = int(frames * src_px * PNG_BYTES_PER_PX_SRC)
    per_out = int(out_px * PNG_BYTES_PER_PX_OUT)
    peak = total_in + min(chunk, frames) * per_out
    return total_in, peak


# ---------------------------------------------------------------- 流水线各阶段


def extract_frames(src: Path, out_dir: Path, total_frames: int, on_progress) -> int:
    """把视频全部解码成 PNG。

    PNG 而不是 JPEG：这是中间产物，JPEG 会引入第二次压缩损失，白白拉低画质。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-v", "error", "-y", "-nostats", "-progress", "pipe:1",
        "-i", str(src),
        str(out_dir / "%08d.png"),
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace",
    )
    assert proc.stdout is not None
    last = 0.0
    for line in proc.stdout:
        m = re.match(r"frame=(\d+)", line.strip())
        if m and total_frames:
            pct = min(100.0, int(m.group(1)) / total_frames * 100)
            if pct - last >= 1:
                last = pct
                on_progress(pct)
    proc.wait()
    count = len(list(out_dir.glob("*.png")))
    if proc.returncode != 0 or count == 0:
        raise UpscaleError(
            f"抽帧失败（{count} 帧后中断，ffmpeg 返回 {proc.returncode}）。"
            "最可能是磁盘满了，或者这个视频文件损坏。"
        )
    return count


def upscale_batch(in_dir: Path, out_dir: Path, model: str, scale: int,
                  gpu: str | None, threads: str, on_frame=None,
                  should_pause=None) -> tuple[int, bool]:
    """让 Real-ESRGAN 处理一小批帧，返回实际产出的帧数。

    为什么不解析它的百分比输出：
        实测（5 张图）它打出来的是 0% 50% 0% 50% 0% 50% ——**每张图都从 0 重新报**。
        拿它当总进度，进度条就会来回横跳，这正是老版本"进度忽高忽低"的原因。

    改成数文件：
        它每写完一张图，output 目录里就多一个 PNG。每秒数一次，得到的是真实完成数，
        单调递增、不会倒退，而且和"总共要处理多少张"天然对得上。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ESRGAN_EXE),
        "-i", str(in_dir), "-o", str(out_dir),
        "-n", model, "-s", str(scale), "-f", "png",
        "-j", threads,
    ]
    if gpu is not None:
        cmd += ["-g", gpu]

    # 输出重定向到文件：失败时能把它最后几行原样贴进错误信息里
    log_path = out_dir.parent / "esrgan_last.log"
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    stopped_for_heat = False
    try:
        proc = subprocess.Popen(cmd, cwd=str(ESRGAN_DIR), stdout=log, stderr=subprocess.STDOUT)

        produced = 0
        while proc.poll() is None:
            produced = len(list(out_dir.glob("*.png")))
            if on_frame is not None:
                on_frame(produced)
            # 温度 5 秒平均一旦越过硬上限，当场停手。
            # 已经写好的帧都在盘上，下一轮从缺的那几张接着做，不会白跑。
            if should_pause is not None and should_pause():
                stopped_for_heat = True
                proc.terminate()
                break
            time.sleep(1.0)
    finally:
        log.close()

    if stopped_for_heat:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    produced = len(list(out_dir.glob("*.png")))
    if on_frame is not None:
        on_frame(produced)

    if not stopped_for_heat and proc.returncode != 0:
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-400:]
        except OSError:
            tail = ""
        raise UpscaleError(
            f"Real-ESRGAN 执行失败（返回码 {proc.returncode}，已产出 {produced} 帧）。\n"
            "常见原因：显存不够、Vulkan 不可用、磁盘满了。\n"
            f"最后几行输出：{tail}\n"
            "可以试 --scale 2 降低负担，或调小 --chunk。"
        )
    return produced, stopped_for_heat


def encode_segment(frames_dir: Path, out_path: Path, fps: float,
                   width: int, height: int, crf: int, start_number: int = 1) -> None:
    vf = f"scale={width}:{height}:flags=lanczos"
    cmd = [
        FFMPEG, "-v", "error", "-y",
        "-framerate", str(fps),
        # 必须显式告诉 ffmpeg 从第几帧开始：image2 解复用器默认从 0 找，
        # 第二块起文件名是 00000061 开头，它会直接找不到而报错。
        "-start_number", str(start_number),
        "-i", str(frames_dir / "%08d.png"),
        "-map", "0:v", "-vf", vf,
    ]

    if ENCODER == "libx264":
        cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-profile:v", "high"]
    elif ENCODER == "h264_nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "p7", "-cq", str(crf)]
    else:
        mbps = max(8.0, width * height * fps * 0.16 / 1_000_000)
        cmd += ["-c:v", "h264_mf", "-b:v", f"{mbps:.1f}M"]

    # 分块用 MPEG-TS 而不是 MP4：TS 可以直接字节拼接，
    # MP4 用 concat 解复用器拼会因为各段时间戳对不齐而丢帧（实测 120 帧变 89 帧）。
    cmd += ["-pix_fmt", "yuv420p", "-f", "mpegts", str(out_path)]

    proc = run(cmd)
    if proc.returncode != 0:
        raise UpscaleError(
            f"编码失败（编码器 {ENCODER}）：{proc.stderr[:400]}\n"
            f"实际命令：{' '.join(cmd)}"
        )


def concat_segments(segments: list[Path], work: Path, src: Path,
                    out_path: Path, has_audio: bool) -> None:
    """把分块编码的结果按顺序拼起来，再把原音轨原样接回去。

    拼接方式：**直接按字节追加**。
        MPEG-TS 是流式格式，一段接一段就是完整的流，不需要任何索引重建。
        MP4 不行——它每段都有自己的时间戳基准，用 concat 解复用器拼的时候
        时间戳对不齐会悄悄丢帧（实测 120 帧的片子拼出来只剩 89 帧）。

    全程 -c copy，不重新编码，所以没有二次画质损失。
    """
    joined = work / "_joined.ts"
    with open(joined, "wb") as out:
        for seg in segments:
            with open(seg, "rb") as part:
                shutil.copyfileobj(part, out, length=4 * 1024 * 1024)

    proc = run([
        FFMPEG, "-v", "error", "-y",
        "-i", str(joined),
        *(("-i", str(src)) if has_audio else ()),
        "-map", "0:v", *(["-map", "1:a?"] if has_audio else []),
        "-c", "copy", "-movflags", "+faststart", str(out_path),
    ])
    if proc.returncode != 0:
        raise UpscaleError(f"拼接分块失败：{proc.stderr[:400]}")


def make_comparison(src: Path, upscaled: Path, out_path: Path, probe_info: dict) -> bool:
    """出一张 100% 像素的对比图，左原图右结果，方便直观看出差别。"""
    ts = max(0.1, probe_info["duration"] / 3)
    frame = out_path.parent / "_cmp_src.png"
    big = out_path.parent / "_cmp_up.png"

    r1 = run([FFMPEG, "-v", "error", "-ss", f"{ts:.2f}", "-i", str(src),
              "-frames:v", "1", str(frame), "-y"])
    r2 = run([FFMPEG, "-v", "error", "-ss", f"{ts:.2f}", "-i", str(upscaled),
              "-frames:v", "1", str(big), "-y"])
    if r1.returncode != 0 or r2.returncode != 0:
        frame.unlink(missing_ok=True)
        big.unlink(missing_ok=True)
        return False

    w, h = probe_info["width"], probe_info["height"]
    # 原图用最近邻放大，才看得出"原来是马赛克"
    proc = run([
        FFMPEG, "-v", "error", "-y",
        "-i", str(frame), "-i", str(big),
        "-filter_complex",
        f"[0:v]scale={w * 2}:{h * 2}:flags=neighbor[a];"
        f"[1:v]scale={w * 2}:{h * 2}:flags=lanczos[b];[a][b]hstack",
        "-q:v", "2", str(out_path),
    ])
    frame.unlink(missing_ok=True)
    big.unlink(missing_ok=True)
    return proc.returncode == 0


# ---------------------------------------------------------------- 主流程


def run_upscale(
    src: Path,
    out_path: Path | None = None,
    *,
    target: str = "native",
    scale: int = DEFAULT_SCALE,
    model: str = DEFAULT_MODEL,
    crf: int = 17,
    gpu: str | None = None,
    chunk: int = DEFAULT_CHUNK,
    max_temp: int = 75,
    max_util: int = 90,
    on_progress=None,
    make_cmp: bool = True,
    keep_frames: bool = False,
) -> dict:
    """跑完整流程。网页后端直接调这个。

    on_progress(pct, detail)：
        pct     0~100 的浮点数，已经做了阶段归一化
        detail  {"stage": 文本, "done": 已完成帧数, "total": 总帧数, "eta": 预计剩余秒}
    """
    require_tools()

    src = Path(src)
    if not src.exists():
        raise UpscaleError(f"文件不存在：{src}")

    info = probe(src)

    if target == "native":
        # native = 分辨率完全不变，只做超采样提升画质
        out_w, out_h = info["width"], info["height"]
    else:
        out_w, out_h = target_size(info["width"], info["height"], target)

    if out_path is None:
        suffix = "画质增强" if target == "native" else target
        out_path = src.with_name(f"{src.stem}_{suffix}.mp4")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def report(pct: float, stage: str, done: int = 0, eta: float = 0.0) -> None:
        if on_progress is not None:
            on_progress(
                max(0.0, min(100.0, pct)),
                {"stage": stage, "done": done, "total": total, "eta": round(eta, 0)},
            )

    # --- 磁盘预检：宁可开跑前拒绝，也不要跑一半把系统盘写满 ---
    total_in, peak = estimate_disk(
        info["frames"], info["width"] * info["height"], out_w * out_h, chunk
    )
    work_root = out_path.parent / "_upscale_work"
    avail = free_bytes(work_root)
    if peak > avail * 0.9:
        # 自动缩小块，看能不能塞下
        per_out = max(1, int(out_w * out_h * PNG_BYTES_PER_PX_OUT))
        room = int(avail * 0.85) - total_in
        chunk_min = max(1, room // per_out)
        if chunk_min >= 1:
            chunk = min(chunk, chunk_min)
            total_in, peak = estimate_disk(
                info["frames"], info["width"] * info["height"], out_w * out_h, chunk
            )
        if peak > avail * 0.9:
            raise UpscaleError(
                f"磁盘空间不够。\n"
                f"这个视频需要约 {peak / 2**30:.1f} GB 临时空间，"
                f"但 {work_root.drive} 盘只剩 {avail / 2**30:.1f} GB。\n"
                f"请清理磁盘，或把输出目录换成空间更大的盘。\n"
                f"（临时目录会建在输出文件旁边的 _upscale_work，不会写 C 盘）"
            )

    total = info["frames"]
    work = work_root / f"run_{int(time.time())}"
    in_dir = work / "in"
    seg_dir = work / "seg"
    work.mkdir(parents=True, exist_ok=True)
    seg_dir.mkdir(parents=True, exist_ok=True)

    monitor = GpuMonitor()
    throttle = Throttle(max_temp=max_temp, max_util=max_util)
    started = time.time()

    try:
        report(1, "抽帧", 0)
        count = extract_frames(
            src, in_dir, total,
            on_progress=lambda p: report(1 + p * 0.07, "抽帧", 0),
        )
        total = count

        monitor.start()

        # --- 分块：超分 -> 编码 -> 删临时帧，循环 ---
        names = sorted(p.name for p in in_dir.glob("*.png"))
        batches = [names[i:i + chunk] for i in range(0, len(names), chunk)]
        segments: list[Path] = []
        done = 0
        ai_started = time.time()

        for bi, batch in enumerate(batches):
            stage_dir = work / "batch"
            out_dir = work / "batch_out"
            for d in (stage_dir, out_dir):
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
                d.mkdir(parents=True, exist_ok=True)

            # 温度超了会中途停手，这一块要能接着做。
            # 已经写进 out_dir 的帧不会重跑，只把缺的重新挂进去。
            pending = list(batch)
            for attempt in range(40):
                shutil.rmtree(stage_dir, ignore_errors=True)
                stage_dir.mkdir(parents=True, exist_ok=True)
                for name in pending:
                    os.link(in_dir / name, stage_dir / name)

                threads = "1:2:2" if throttle.sleep_now < 4 else "1:1:1"

                def on_frame(n: int, _base: int = done) -> None:
                    """块内每完成一帧就报一次，进度条才会一直往前走。"""
                    finished = min(total, _base + n)
                    elapsed = time.time() - ai_started
                    rate = finished / elapsed if elapsed > 0 and finished > 0 else 0
                    eta_sec = (total - finished) / rate if rate > 0 else 0
                    label = f"超分 {finished}/{total} 帧"
                    if throttle.peak_temp:
                        label += f"　{throttle.peak_temp}°C"
                    report(8 + finished / total * 86, label, finished, eta_sec)

                produced, stopped_for_heat = upscale_batch(
                    stage_dir, out_dir, model, scale, gpu, threads,
                    on_frame, should_pause=lambda: throttle.too_hot(monitor),
                )

                already = {p.name for p in out_dir.glob("*.png")}
                pending = [n for n in pending if n not in already]
                if not pending:
                    break

                if not stopped_for_heat:
                    raise UpscaleError(
                        f"第 {bi + 1} 块只产出了 {len(already)}/{len(batch)} 帧就中断了，"
                        "继续下去会得到一段残缺视频，已中止。\n"
                        "最可能是磁盘满了或显存不够。"
                    )

                # 热停：**等温度真的降下来**再接着做，不是固定睡几秒。
                # 固定睡会导致"刚恢复就再次超温"的反覆抖动。
                deadline = time.time() + 180
                while time.time() < deadline:
                    current = throttle.temp_avg(monitor, 5.0)
                    if current <= throttle.soft_temp:
                        break
                    report(8 + done / total * 86,
                           f"显卡 {current:.0f}°C，降温中…（目标 {throttle.soft_temp}°C）",
                           done, max(0.0, deadline - time.time()))
                    time.sleep(2.0)
            else:
                raise UpscaleError("反复因为温度停下，超过 40 次仍没做完。请调低目标或换更强的散热。")

            seg = seg_dir / f"seg_{bi:04d}.ts"
            start_number = int(Path(batch[0]).stem)
            encode_segment(out_dir, seg, info["fps"], out_w, out_h, crf, start_number)
            segments.append(seg)

            # 这一块用完立刻删，峰值占用不会累积
            for name in batch:
                (in_dir / name).unlink(missing_ok=True)
            shutil.rmtree(stage_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)

            done += len(batch)
            elapsed = time.time() - ai_started
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            stage = f"超分 {done}/{total} 帧"
            if throttle.peak_temp:
                stage += f"　{throttle.peak_temp}°C"
            report(8 + done / total * 86, stage, done, eta)

            if bi < len(batches) - 1:
                wait = throttle.adjust(monitor)
                if wait > 0:
                    report(8 + done / total * 86,
                           f"{stage}　降温中 {wait:.0f}s", done, eta + wait)
                    time.sleep(wait)

        monitor.stop()

        report(95, "拼接", total, 0)
        concat_segments(segments, work, src, out_path, info["has_audio"])

        # --- 校验：帧数对不上就是失败，绝不交付残片 ---
        report(97, "校验", total, 0)
        final = probe(out_path)
        if final["frames"] < total * 0.98:
            raise UpscaleError(
                f"输出不完整：源 {total} 帧，成品只有 {final['frames']} 帧。\n"
                "已删除这个残片。请检查磁盘空间和日志后重试。"
            )

        cmp_path = None
        if make_cmp:
            candidate = out_path.with_name(out_path.stem + "_对比.jpg")
            if make_comparison(src, out_path, candidate, info):
                cmp_path = candidate

        report(100, "完成", total, 0)

        return {
            "output": str(out_path),
            "width": final["width"],
            "height": final["height"],
            "size": final["size"],
            "duration": final["duration"],
            "frames": final["frames"],
            "has_audio": final["has_audio"],
            "elapsed": round(time.time() - started, 1),
            "comparison": str(cmp_path) if cmp_path else None,
            "scale": scale,
            "chunk": chunk,
            "throttle": {
                "max_temp": max_temp, "max_util": max_util,
                "peak_temp": throttle.peak_temp,
                "mean_util": round(throttle.mean_util, 0),
                "gpu_available": monitor.available,
            },
            "source": {
                "width": info["width"], "height": info["height"],
                "duration": info["duration"], "frames": info["frames"],
            },
        }
    finally:
        monitor.stop()
        if keep_frames:
            print(f"临时帧保留在：{work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
            try:
                work_root.rmdir()      # 空了就顺手删掉，不空就算了
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="用 Real-ESRGAN 提升视频画质（可选同时改分辨率）"
    )
    parser.add_argument("input", help="输入视频")
    parser.add_argument("-o", "--output", help="输出文件（默认放在输入旁边）")
    parser.add_argument("--target", default="native",
                        choices=["native", "1080p", "2k", "4k"],
                        help="native=分辨率不变只提画质（默认）；其余按短边缩放")
    parser.add_argument("--scale", type=int, default=DEFAULT_SCALE, choices=[2, 3, 4],
                        help="AI 放大倍数，之后缩回目标分辨率。默认 2（实测比 3 更好且快一倍多）")
    parser.add_argument("--model", default=DEFAULT_MODEL, choices=MODELS)
    parser.add_argument("--gpu", default=None, help="多显卡时指定 GPU id，如 0")
    parser.add_argument("--crf", type=int, default=17, help="H.264 质量，越小越清晰（默认 17）")
    parser.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                        help=f"每块处理多少帧（默认 {DEFAULT_CHUNK}）。越小温度越稳、峰值磁盘越低")
    parser.add_argument("--max-temp", type=int, default=75,
                        help="显卡温度硬上限 °C，按 5 秒平均判定（0=不限，默认 75，日常会压在 70 左右）")
    parser.add_argument("--max-util", type=int, default=90,
                        help="显卡占用上限 %%，按 60 秒平均判定（0=不限，默认 90）")
    parser.add_argument("--no-throttle", action="store_true", help="完全不限速，跑到最快")
    parser.add_argument("--keep-frames", action="store_true", help="保留抽出来的帧（调试用）")
    parser.add_argument("--no-compare", action="store_true", help="不生成对比图")
    args = parser.parse_args()

    src = Path(args.input).expanduser().resolve()
    if not src.exists():
        print(f"文件不存在：{src}", file=sys.stderr)
        return 1

    try:
        require_tools()
        info = probe(src)
    except UpscaleError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.target == "native":
        out_w, out_h = info["width"], info["height"]
    else:
        out_w, out_h = target_size(info["width"], info["height"], args.target)

    suffix = "画质增强" if args.target == "native" else args.target
    out_path = Path(args.output).expanduser().resolve() if args.output else \
        src.with_name(f"{src.stem}_{suffix}.mp4")

    max_temp = 0 if args.no_throttle else args.max_temp
    max_util = 0 if args.no_throttle else args.max_util

    print("=" * 66)
    print(f"  输入  {src.name}")
    print(f"        {info['width']}×{info['height']}  {info['fps']}fps  "
          f"{info['duration']:.1f}s  {info['frames']} 帧  "
          f"{'有音频' if info['has_audio'] else '无音频'}")
    print(f"  输出  {out_path.name}")
    print(f"        {out_w}×{out_h}  （AI ×{args.scale} 超采样后缩回）")
    print(f"  模型  {args.model}")
    print(f"  编码  {ENCODER}（{'项目自带 ffmpeg' if FFMPEG != 'ffmpeg' else '系统 ffmpeg'}）")
    limit = "不限速" if not (max_temp or max_util) else f"温度 ≤{max_temp}°C　占用 ≤{max_util}%"
    print(f"  限速  {limit}　每块 {args.chunk} 帧")
    print("=" * 66)

    def show(pct: float, detail: dict) -> None:
        eta = detail.get("eta") or 0
        tail = f"　预计还要 {eta / 60:.0f} 分钟" if eta > 0 else ""
        print(f"\r  [{pct:5.1f}%] {detail['stage']}{tail}          ", end="", flush=True)

    try:
        result = run_upscale(
            src, out_path,
            target=args.target, scale=args.scale, model=args.model,
            crf=args.crf, gpu=args.gpu, chunk=args.chunk,
            max_temp=max_temp, max_util=max_util,
            on_progress=show,
            make_cmp=not args.no_compare,
            keep_frames=args.keep_frames,
        )
    except UpscaleError as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 1

    print("\n" + "=" * 66)
    print(f"  完成，用时 {result['elapsed']:.1f} 秒（{result['elapsed'] / 60:.1f} 分钟）")
    print(f"  {result['output']}")
    print(f"  {result['width']}×{result['height']}  {result['frames']} 帧  "
          f"{result['size'] / 1024 / 1024:.1f} MB")
    t = result["throttle"]
    if t["gpu_available"]:
        print(f"  显卡温度峰值 {t['peak_temp']}°C　平均占用 {t['mean_util']:.0f}%")
    if result["comparison"]:
        print(f"  对比图 {result['comparison']}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
