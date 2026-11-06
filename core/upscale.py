"""视频超清任务管理。

网页上点「超清」之后跑的就是这里。真正的处理在 tools/upscale_video.py，
这里只负责：排队、跑线程、把进度报给前端、记住结果。

为什么任务要落盘：
    以前任务只存在内存里，服务一重启就全没了。可是成品视频明明还在磁盘上，
    界面上却再也找不到入口——用户点「下载」只会拿到 404。
    现在每个任务都会写一份 JSON 到 _upscale/jobs/，重启后重新读回来，
    状态是 running 的会被标成「已中断」，已经完成的照样能播放、能下载。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tools import upscale_video

TERMINAL = {"done", "failed", "canceled"}


@dataclass
class UpscaleJob:
    id: str
    source: str
    source_label: str
    target: str
    scale: int
    model: str
    output: str
    status: str = "queued"
    progress: float = 0.0
    stage: str = "排队中"
    done: int = 0
    total: int = 0
    eta: float = 0.0
    comparison: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "source_label": self.source_label,
            "target": self.target,
            "scale": self.scale,
            "model": self.model,
            "status": self.status,
            "progress": round(self.progress, 1),
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "eta": int(self.eta or 0),
            "output": self.output if self.status == "done" else None,
            "comparison": self.comparison,
            "error": self.error,
            "result": self.result,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "has_video": Path(self.output).exists() if self.output else False,
        }

    def dump(self) -> dict[str, Any]:
        return {
            "id": self.id, "source": self.source, "source_label": self.source_label,
            "target": self.target, "scale": self.scale, "model": self.model,
            "output": self.output, "status": self.status, "progress": self.progress,
            "stage": self.stage, "done": self.done, "total": self.total, "eta": self.eta,
            "comparison": self.comparison, "error": self.error, "result": self.result,
            "created_at": self.created_at, "finished_at": self.finished_at,
        }


class UpscaleManager:
    def __init__(self, jobs_dir: Path | None = None) -> None:
        self._jobs: dict[str, UpscaleJob] = {}
        self._lock = threading.Lock()
        self.jobs_dir = jobs_dir
        if jobs_dir is not None:
            jobs_dir.mkdir(parents=True, exist_ok=True)
            self._load()

    # ---------------------------------------------------------------- 落盘

    def _path(self, job_id: str) -> Path | None:
        return None if self.jobs_dir is None else self.jobs_dir / f"{job_id}.json"

    def _save(self, job: UpscaleJob) -> None:
        path = self._path(job.id)
        if path is None:
            return
        try:
            path.write_text(json.dumps(job.dump(), ensure_ascii=False, indent=1),
                            encoding="utf-8")
        except OSError:
            pass    # 落盘失败不影响任务本身

    def _load(self) -> None:
        """把上次运行留下的任务读回来。

        中断的（running/queued）标成「已中断」——因为它早就死了，
        不能让它一直显示"进行中"，否则前端会永远轮询下去。
        成品文件还在的，状态改成 done，这样还能播放和下载。
        """
        if self.jobs_dir is None:
            return
        for path in sorted(self.jobs_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = UpscaleJob(**{k: v for k, v in data.items()
                                    if k in UpscaleJob.__dataclass_fields__})
            except (OSError, ValueError, TypeError):
                continue

            if job.status not in TERMINAL:
                if Path(job.output).exists() and job.progress >= 95:
                    job.status = "done"
                    job.stage = "完成"
                    job.progress = 100
                else:
                    job.status = "failed"
                    job.stage = "已中断"
                    job.error = "服务重启时这个任务还没跑完，已被中断。成品文件不完整，请重新处理。"

            if job.status == "done" and not Path(job.output).exists():
                job.status = "failed"
                job.stage = "已中断"
                job.error = "成品文件已不在磁盘上（可能被清理了）。"

            self._jobs[job.id] = job

    # ---------------------------------------------------------------- 接口

    def submit(
        self,
        source: Path,
        output: Path,
        *,
        target: str = "native",
        scale: int = upscale_video.DEFAULT_SCALE,
        model: str = upscale_video.DEFAULT_MODEL,
        label: str = "",
        chunk: int = upscale_video.DEFAULT_CHUNK,
        max_temp: int = 75,
        max_util: int = 90,
    ) -> UpscaleJob:
        job = UpscaleJob(
            id=uuid.uuid4().hex[:12],
            source=str(source),
            source_label=label or source.name,
            target=target,
            scale=scale,
            model=model,
            output=str(output),
        )
        with self._lock:
            self._jobs[job.id] = job
        self._save(job)

        threading.Thread(
            target=self._run,
            args=(job, chunk, max_temp, max_util),
            daemon=True,
        ).start()
        return job

    def get(self, job_id: str) -> UpscaleJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self) -> list[UpscaleJob]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status not in TERMINAL]

    def history(self, limit: int = 50) -> list[UpscaleJob]:
        """所有任务，最新的在前。给前端的「历史超清」列表用。"""
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def forget(self, job_id: str) -> bool:
        """只删记录，不删成品文件。"""
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        path = self._path(job_id)
        if path is not None:
            path.unlink(missing_ok=True)
        return True

    # ---------------------------------------------------------------- 执行

    def _run(self, job: UpscaleJob, chunk: int, max_temp: int, max_util: int) -> None:
        job.status = "running"
        job.stage = "准备中"
        self._save(job)

        def on_progress(pct: float, detail: dict | None = None) -> None:
            job.progress = pct
            detail = detail or {}
            job.stage = detail.get("stage") or job.stage
            if detail:
                job.done = int(detail.get("done") or 0)
                job.total = int(detail.get("total") or 0)
                job.eta = float(detail.get("eta") or 0)

        try:
            result = upscale_video.run_upscale(
                Path(job.source),
                Path(job.output),
                target=job.target,
                scale=job.scale,
                model=job.model,
                chunk=chunk,
                max_temp=max_temp,
                max_util=max_util,
                on_progress=on_progress,
                make_cmp=True,
            )
            job.result = result
            job.comparison = result.get("comparison")
            job.progress = 100
            job.stage = "完成"
            job.done = job.total = result.get("frames") or 0
            job.eta = 0
            job.status = "done"
            job.finished_at = datetime.now().isoformat(timespec="seconds")
        except Exception as exc:  # noqa: BLE001 - 任何失败都要让前端看到原因
            job.status = "failed"
            job.stage = "失败"
            job.error = str(exc)
            job.finished_at = datetime.now().isoformat(timespec="seconds")
        finally:
            self._save(job)
