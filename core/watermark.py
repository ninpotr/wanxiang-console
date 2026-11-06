"""AI 水印去除任务管理。

网页「后处理 → AI 去水印」跑的就是这里。真正的推理在隔壁
`video-subtitle-remover/wm_web_runner.py`，用那个项目自己的 `.venv` 启动，
避免把 Paddle / PyTorch 塞进控制台的运行环境。

runner 直接使用 VSR 原生的 SubtitleRemover，所以算法（STTN / ProPainter /
LAMA / OpenCV）、检测模型、STTN/ProPainter 参数、多选框和 A/B 区间都和
桌面版一致；另外保留一个不跑 OCR 的「LAMA 直接框选」用于纯 logo 水印。

任务落盘到 `_watermark/jobs/`：服务重启后历史还能查、成品还能下载；
当时还在跑的会被标成「已中断」。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
# 默认假设 video-subtitle-remover 与控制台同级；也可以用 WAN3_VSR_ROOT 指定。
VSR_ROOT = Path(
    os.environ.get("WAN3_VSR_ROOT")
    or (ROOT.parent / "video-subtitle-remover")
)
VSR_PYTHON = VSR_ROOT / ".venv" / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python"
)
# 网页执行器随控制台一起发布，不要求用户改 VSR 仓库。
WM_RUNNER = ROOT / "tools" / "watermark" / "wm_web_runner.py"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".wmv", ".ts"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

TERMINAL = {"done", "failed", "canceled"}
PROGRESS_PREFIX = "@@WM_PROGRESS "
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class WatermarkError(RuntimeError):
    pass


@dataclass
class WatermarkJob:
    id: str
    source: str
    source_type: str
    source_label: str
    media_type: str
    mode: str
    detect_model: str
    preset: str = "auto"
    engine: str = ""
    areas: list[list[float]] | None = None
    ab: list[float] | None = None
    auto_detect: bool = False
    hardware_acceleration: bool = True
    advanced: dict[str, Any] | None = None
    detect_only: bool = False
    box: list[float] | None = None
    output: str = ""
    preview: str = ""
    live_preview: str = ""
    status: str = "queued"
    progress: float = 0.0
    stage: str = "排队中"
    done: int = 0
    total: int = 0
    error: str | None = None
    result: dict[str, Any] | None = None
    log_tail: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str | None = None

    # 运行期状态，不落盘
    proc: Any = field(default=None, repr=False, compare=False)
    cancel_requested: bool = field(default=False, repr=False, compare=False)

    @property
    def output_path(self) -> Path:
        return Path(self.output)

    @property
    def preview_path(self) -> Path:
        return Path(self.preview)

    @property
    def live_preview_path(self) -> Path:
        return Path(self.live_preview) if self.live_preview else Path()

    @property
    def log_path(self) -> Path:
        return Path(self.output).with_suffix(".log")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "source_type": self.source_type,
            "source_label": self.source_label,
            "media_type": self.media_type,
            "mode": self.mode,
            "engine": self.engine or self.mode,
            "detect_model": self.detect_model,
            "preset": self.preset,
            "areas": self.areas,
            "ab": self.ab,
            "auto_detect": self.auto_detect,
            "hardware_acceleration": self.hardware_acceleration,
            "advanced": self.advanced,
            "detect_only": self.detect_only,
            "box": self.box,
            "status": self.status,
            "progress": round(self.progress, 1),
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "error": self.error,
            "result": self.result,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "has_file": self.output_path.exists() if not self.detect_only else False,
            "has_preview": self.preview_path.exists() if self.preview else False,
            "has_live_preview": (
                bool(self.live_preview) and self.live_preview_path.exists()
                and not self.detect_only
            ),
            "log_tail": self.log_tail[-60:],
        }

    def dump(self) -> dict[str, Any]:
        return {
            "id": self.id, "source": self.source, "source_type": self.source_type,
            "source_label": self.source_label, "media_type": self.media_type,
            "mode": self.mode, "detect_model": self.detect_model,
            "preset": self.preset, "engine": self.engine,
            "areas": self.areas, "ab": self.ab, "auto_detect": self.auto_detect,
            "hardware_acceleration": self.hardware_acceleration,
            "advanced": self.advanced, "detect_only": self.detect_only,
            "box": self.box, "output": self.output, "preview": self.preview,
            "live_preview": self.live_preview,
            "status": self.status, "progress": self.progress, "stage": self.stage,
            "done": self.done, "total": self.total, "error": self.error,
            "result": self.result, "log_tail": self.log_tail[-80:],
            "created_at": self.created_at, "finished_at": self.finished_at,
        }


class WatermarkManager:
    def __init__(self, jobs_dir: Path | None = None) -> None:
        self._jobs: dict[str, WatermarkJob] = {}
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()   # 同一时间只跑一个，避免 12G 显存互相挤
        self.jobs_dir = jobs_dir
        if jobs_dir is not None:
            jobs_dir.mkdir(parents=True, exist_ok=True)
            self._load()

    # ---------------------------------------------------------------- 落盘

    def _path(self, job_id: str) -> Path | None:
        return None if self.jobs_dir is None else self.jobs_dir / f"{job_id}.json"

    def _spec_path(self, job_id: str) -> Path | None:
        return None if self.jobs_dir is None else self.jobs_dir / f"{job_id}.spec.json"

    def _save(self, job: WatermarkJob) -> None:
        path = self._path(job.id)
        if path is None:
            return
        try:
            path.write_text(json.dumps(job.dump(), ensure_ascii=False, indent=1),
                            encoding="utf-8")
        except OSError:
            pass

    def _load(self) -> None:
        if self.jobs_dir is None:
            return
        for path in sorted(self.jobs_dir.glob("*.json")):
            if path.name.endswith(".spec.json"):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                # 兼容旧版本任务 JSON（当时只有 engine，没有 mode）
                data.setdefault("mode", data.get("engine") or "sttn-auto")
                data.setdefault("detect_model", "fast")
                data.setdefault("media_type", "video")
                job = WatermarkJob(**{k: v for k, v in data.items()
                                      if k in WatermarkJob.__dataclass_fields__})
            except (OSError, ValueError, TypeError):
                continue

            if job.status not in TERMINAL:
                ready = (job.preview_path.exists() if job.detect_only
                         else job.output_path.exists())
                if ready and job.progress >= 95:
                    job.status = "done"
                    job.stage = "完成" if not job.detect_only else "检测完成"
                    job.progress = 100
                else:
                    job.status = "failed"
                    job.stage = "已中断"
                    job.error = "服务重启时这个任务还没跑完，已被中断。请重新处理。"
            self._jobs[job.id] = job

    # ---------------------------------------------------------------- 接口

    def submit(
        self,
        source: Path,
        *,
        source_type: str,
        label: str,
        media_type: str,
        mode: str = "sttn-auto",
        detect_model: str = "fast",
        preset: str = "auto",
        engine: str = "",
        areas: list[list[float]] | None = None,
        ab: list[float] | None = None,
        auto_detect: bool = False,
        hardware_acceleration: bool = True,
        advanced: dict[str, Any] | None = None,
        detect_only: bool = False,
        box: list[float] | None = None,
        output_dir: Path,
    ) -> WatermarkJob:
        if not VSR_PYTHON.exists():
            raise WatermarkError(
                f"找不到去水印环境：{VSR_PYTHON}。\n"
                "请按 SETUP.md 在 video-subtitle-remover 目录创建 .venv，"
                "或用环境变量 WAN3_VSR_ROOT 指向它。"
            )
        if not WM_RUNNER.exists():
            raise WatermarkError(f"找不到去水印执行器：{WM_RUNNER}")

        job_id = uuid.uuid4().hex[:12]
        suffix = source.suffix.lower()
        out_suffix = suffix if media_type == "image" else ".mp4"
        output_dir.mkdir(parents=True, exist_ok=True)

        job = WatermarkJob(
            id=job_id,
            source=str(source),
            source_type=source_type,
            source_label=label or source.name,
            media_type=media_type,
            mode=mode,
            detect_model=detect_model,
            preset=preset,
            engine=engine,
            areas=areas,
            ab=ab,
            auto_detect=auto_detect,
            hardware_acceleration=hardware_acceleration,
            advanced=advanced,
            detect_only=detect_only,
            box=box,
            output=str(output_dir / f"wm_{job_id}{out_suffix}"),
            preview=str(output_dir / f"wm_{job_id}_detect.jpg"),
            live_preview=str(output_dir / f"wm_{job_id}_live.jpg"),
        )

        spec = {
            "input": job.source,
            "output": job.output,
            "media_type": media_type,
            "mode": mode,
            "detect_model": detect_model,
            "areas": areas or [],
            "ab": ab,
            "auto_detect": auto_detect,
            "hardware_acceleration": hardware_acceleration,
            "advanced": advanced or {},
            "detect_only": detect_only,
            "preview": job.preview,
            "live_preview": job.live_preview if not detect_only else job.preview,
        }
        spec_path = self._spec_path(job.id)
        if spec_path is not None:
            spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1),
                                 encoding="utf-8")

        with self._lock:
            self._jobs[job.id] = job
        self._save(job)

        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def get(self, job_id: str) -> WatermarkJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self) -> list[WatermarkJob]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status not in TERMINAL]

    def history(self, limit: int = 50) -> list[WatermarkJob]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def forget(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        for path in (self._path(job_id), self._spec_path(job_id)):
            if path is not None:
                path.unlink(missing_ok=True)
        return True

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status in TERMINAL:
            return False
        job.cancel_requested = True
        job.stage = "正在停止"
        proc = job.proc
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True, timeout=15,
                    )
                else:
                    proc.terminate()
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:
                    pass
        else:
            # 还在排队，没有子进程，直接标记
            job.status = "canceled"
            job.stage = "已停止"
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            self._save(job)
        return True

    # ---------------------------------------------------------------- 执行

    def _run(self, job: WatermarkJob) -> None:
        with self._run_lock:
            if self.get(job.id) is None:
                return
            if job.cancel_requested:
                job.status = "canceled"
                job.stage = "已停止"
                self._save(job)
                return
            job.status = "running"
            job.stage = "启动处理"
            self._save(job)

            spec_path = self._spec_path(job.id)
            if spec_path is None or not spec_path.exists():
                job.status = "failed"
                job.stage = "失败"
                job.error = "任务参数文件不存在。"
                self._save(job)
                return

            cmd = [str(VSR_PYTHON), str(WM_RUNNER), "--spec", str(spec_path)]
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            env["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "1"
            env["WAN3_VSR_ROOT"] = str(VSR_ROOT)

            log_path = job.log_path
            log_path.parent.mkdir(parents=True, exist_ok=True)
            return_code = -1
            try:
                with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(VSR_ROOT),
                        stdout=subprocess.PIPE,
                        stderr=log_file,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        env=env,
                        creationflags=CREATE_NO_WINDOW,
                    )
                    job.proc = proc
                    assert proc.stdout is not None
                    for raw in proc.stdout:
                        line = raw.rstrip("\r\n")
                        if not line:
                            continue
                        if PROGRESS_PREFIX in line:
                            before, after = line.split(PROGRESS_PREFIX, 1)
                            self._apply_progress(job, after)
                            line = before.strip()
                            if not line or line.startswith(("Subtitle Removing:", "Subtitle Finding:")):
                                continue
                        line = ANSI_RE.sub("", line).strip()
                        if not line or "QFluentWidgets Pro" in line:
                            continue
                        # tqdm 的进度条本身不放进日志面板，GUI 也不显示它
                        if line.startswith(("Subtitle Removing:", "Subtitle Finding:")):
                            continue
                        job.log_tail.append(line)
                        if len(job.log_tail) > 120:
                            job.log_tail = job.log_tail[-120:]
                        self._save(job)
                    return_code = proc.wait()
            except OSError as exc:
                job.status = "failed"
                job.stage = "失败"
                job.error = f"启动处理进程失败：{exc}"
                job.finished_at = datetime.now().isoformat(timespec="seconds")
                self._save(job)
                return
            finally:
                job.proc = None

            job.finished_at = datetime.now().isoformat(timespec="seconds")
            if job.cancel_requested:
                job.status = "canceled"
                job.stage = "已停止"
                job.error = None
            elif return_code == 0:
                self._finish_ok(job)
            else:
                job.status = "failed"
                job.stage = "失败"
                detail = self._error_from_log(log_path)
                job.error = detail or f"处理进程退出码 {return_code}，请查看运行日志。"
            self._save(job)

    def _apply_progress(self, job: WatermarkJob, payload: str) -> None:
        try:
            data = json.loads(payload)
        except ValueError:
            return
        if "progress" in data:
            job.progress = float(data["progress"])
        if data.get("stage"):
            job.stage = str(data["stage"])
        if "done" in data:
            try:
                job.done = int(data["done"])
            except (TypeError, ValueError):
                pass
        if "total" in data:
            try:
                job.total = int(data["total"])
            except (TypeError, ValueError):
                pass
        if data.get("boxes"):
            job.result = job.result or {}
            job.result["boxes"] = data["boxes"]
        if data.get("width"):
            job.result = job.result or {}
            job.result["width"] = data["width"]
        if data.get("height"):
            job.result = job.result or {}
            job.result["height"] = data["height"]
        if data.get("preview"):
            job.preview = str(data["preview"])
        if data.get("output"):
            job.output = str(data["output"])
        self._save(job)

    def _finish_ok(self, job: WatermarkJob) -> None:
        job.progress = 100
        if job.detect_only:
            if not job.preview_path.exists():
                job.status = "failed"
                job.stage = "失败"
                job.error = "检测完成但没有生成预览图。"
                return
            job.status = "done"
            job.stage = "检测完成"
            job.result = job.result or {}
            job.result["preview"] = job.preview
            return

        if not job.output_path.exists():
            job.status = "failed"
            job.stage = "失败"
            job.error = "处理进程结束了，但没有找到输出文件。"
            return
        job.status = "done"
        job.stage = "完成"
        job.result = job.result or {}
        job.result["bytes"] = job.output_path.stat().st_size

    @staticmethod
    def _error_from_log(log_path: Path) -> str | None:
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines[-120:]):
            text = line.strip()
            if any(key in text for key in ("[失败]", "Error", "Traceback", "RuntimeError", "Exception")):
                return text[:500]
        return None
