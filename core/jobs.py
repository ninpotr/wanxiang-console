"""任务编排。

一条任务的完整链路：
    预检 -> 建目录 -> 复制参考素材 -> 上传换 oss:// -> 拼请求 -> 落 request.json
    -> 创建任务 -> 轮询 -> 立刻下载（URL 只活 24 小时）-> 算实际成本 -> 落 meta.json

用线程跑，因为整条链路以阻塞式 HTTP 为主，而且本地单人使用没有并发压力。
"""

from __future__ import annotations

import hashlib
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import dashscope, logging_setup, pricing, probe, rules, store, uploader
from .config import DURATION_SMART, load_settings

logger = logging_setup.get_logger()

TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}


@dataclass
class Job:
    id: str
    job_dir: Path
    status: str = "queued"
    step: str = "排队中"
    error: str | None = None
    progress: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    log: list[dict[str, str]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    cancel_requested: bool = False

    def note(self, message: str) -> None:
        self.log.append(
            {"time": datetime.now().strftime("%H:%M:%S"), "message": message}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dir": self.job_dir.name,
            "status": self.status,
            "step": self.step,
            "progress": self.progress,
            "error": self.error,
            "created_at": self.created_at,
            "log": self.log,
            "meta": self.meta,
            "video_url": f"/api/jobs/{self.id}/video" if self.status == "succeeded" else None,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        # 同一次运行里同一个文件+同一个模型只传一次
        self._upload_cache: dict[str, str] = {}

    # ---------- 对外接口 ----------

    def submit(self, payload: dict[str, Any]) -> Job:
        settings = load_settings()
        job_id = uuid.uuid4().hex[:12]
        job_dir = store.new_job_dir(settings, payload.get("prompt") or "未命名")
        job = Job(id=job_id, job_dir=job_dir)

        with self._lock:
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run, args=(job, payload, settings), daemon=True
        )
        thread.start()
        return job

    def resume_orphaned_jobs(self, settings: dict[str, Any]) -> list[str]:
        """启动时把中断的任务接回来。

        任务一旦提交成功，就由阿里那边在跑了（task_id 24 小时内有效）。
        但本地的轮询线程是随进程走的 —— 服务一重启，轮询没了，任务既不会
        被查询也不会被下载，用户就这么干等着。

        启动时扫一遍 outputs/，找出「已提交但没拿到视频」的任务，重新接上。
        """
        api_key = (settings.get("api_key") or "").strip()
        if not api_key:
            return []

        from .config import get_base_url

        try:
            base_url = get_base_url(settings)
        except ValueError:
            return []

        try:
            root = store.output_root(settings)
        except Exception:  # noqa: BLE001
            return []

        candidates: list[Path] = []
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if (entry / "video.mp4").exists():
                continue  # 已经下载好了
            meta = store.read_meta(entry)
            if not meta or not meta.get("task_id"):
                continue  # 没提交过，或者本来就没拿到 task_id
            if meta.get("status") in ("succeeded", "failed", "canceled"):
                continue
            candidates.append(entry)

        if not candidates:
            return []

        # 按提交时间的旧→新处理，早点把快到 24 小时的抢救回来
        candidates.sort(key=lambda p: p.stat().st_mtime)

        recovered: list[str] = []
        for job_dir in candidates:
            job_id = uuid.uuid4().hex[:12]
            job = Job(id=job_id, job_dir=job_dir, status="running",
                      step="恢复中断的任务", progress=40)
            with self._lock:
                self._jobs[job_id] = job
            job.note("检测到上次中断的任务，重新接上轮询")

            thread = threading.Thread(
                target=self._resume_one,
                args=(job, settings, base_url, job_dir),
                daemon=True,
            )
            thread.start()
            recovered.append(job_dir.name)
            logger.info("恢复中断任务 | 目录=%s", job_dir.name)

        return recovered

    def _resume_one(
        self,
        job: Job,
        settings: dict[str, Any],
        base_url: str,
        job_dir: Path,
    ) -> None:
        """接回一条中断的任务：查状态 -> 没完就继续轮询 -> 完了就下载。"""
        api_key = (settings.get("api_key") or "").strip()
        meta = store.read_meta(job_dir) or {}
        task_id = meta.get("task_id")
        model = meta.get("model") or settings.get("model")

        try:
            poll_interval = int(settings.get("poll_interval_seconds", 15))
            ticks = {"n": 0}

            def on_tick(result: dict[str, Any]) -> None:
                ticks["n"] += 1
                job.progress = min(88, 40 + ticks["n"] * 3)
                job.step = f"恢复中 · 第 {ticks['n']} 次查询 · {result.get('task_status')}"

            finished = dashscope.poll_until_done(
                base_url, api_key, task_id,
                interval=poll_interval, on_tick=on_tick,
            )

            output = finished.get("output") or {}
            usage = finished.get("usage") or {}
            meta["task_status"] = finished.get("task_status")
            meta["usage"] = usage
            meta["orig_prompt"] = output.get("orig_prompt")
            meta.setdefault("request_ids", {})["恢复查询"] = finished.get("request_id")

            if finished.get("task_status") != "SUCCEEDED":
                meta["status"] = "failed"
                meta["error"] = (
                    output.get("message") or output.get("code")
                    or f"任务状态 {finished.get('task_status')}"
                    + ("（task_id 超过 24 小时已失效）"
                       if finished.get("task_status") == "UNKNOWN" else "")
                )
                store.write_meta(job_dir, meta)
                job.status = "failed"
                job.error = meta["error"]
                job.step = "失败"
                job.note(f"恢复失败：{meta['error']}")
                return

            video_url = output.get("video_url")
            if not video_url:
                raise RuntimeError("任务成功但没返回 video_url")

            job.step = "补下载视频"
            job.progress = 90
            dest = job_dir / "video.mp4"
            size = dashscope.download_video(
                video_url, dest, retries=int(settings.get("download_retries", 3))
            )

            meta["status"] = "succeeded"
            meta["video"] = {
                "file": "video.mp4",
                "size_bytes": size,
                "source_url": video_url,
                "note": "服务重启导致轮询中断，启动时自动接回并补下载。",
            }
            meta["cost"] = pricing.actual_cost(settings, model, usage)
            store.write_meta(job_dir, meta)

            job.status = "succeeded"
            job.step = "完成"
            job.progress = 100
            job.meta = meta
            job.note(f"已接回并下载：video.mp4（{size / 1024 / 1024:.1f} MB）")
            logger.info("中断任务已恢复 | 目录=%s | %s bytes", job_dir.name, size)

        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)
            job.step = "恢复失败"
            job.note(f"恢复失败：{exc}")
            meta["status"] = "failed"
            meta["error"] = f"恢复中断的任务时失败：{exc}"
            store.write_meta(job_dir, meta)
            logger.error("恢复中断任务失败 | 目录=%s | %s", job_dir.name, exc)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active_jobs(self) -> list[Job]:
        """还没结束的任务，按创建时间排。"""
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.status not in TERMINAL_STATUSES]
        jobs.sort(key=lambda j: j.created_at)
        return jobs

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return False
        job.cancel_requested = True
        job.note("已请求取消，等待当前步骤结束")
        return True

    # ---------- 主流程 ----------

    def _run(self, job: Job, payload: dict[str, Any], settings: dict[str, Any]) -> None:
        logger.info(
            "任务开始 | job=%s | 目录=%s | 模型=%s | 分辨率=%s | 时长=%s | 水印=%s",
            job.id, job.job_dir.name, payload.get("model"), payload.get("resolution"),
            payload.get("duration"), bool(payload.get("watermark", False)),
        )
        try:
            self._execute(job, payload, settings)
            logger.info(
                "任务完成 | job=%s | 花费=%s | 计费秒数=%s",
                job.id,
                (job.meta.get("cost") or {}).get("amount"),
                (job.meta.get("usage") or {}).get("duration"),
            )
        except Exception as exc:  # noqa: BLE001 - 兜底，任何异常都要落盘并展示
            job.status = "failed"
            job.error = str(exc)
            job.step = "失败"
            job.note(f"失败：{exc}")
            job.meta.setdefault("error", str(exc))
            job.meta.setdefault("traceback", traceback.format_exc()[-2000:])
            # 完整堆栈进日志文件；meta.json 里只留尾部，免得文件太大
            logger.error("任务失败 | job=%s | %s", job.id, exc)
            logger.error("失败详情 | job=%s |\n%s", job.id, traceback.format_exc())
            self._persist(job)

    def _execute(self, job: Job, payload: dict[str, Any], settings: dict[str, Any]) -> None:
        api_key = (settings.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("还没配置 API Key，请先在「设置」里填写。")

        from .config import get_base_url

        base_url = get_base_url(settings)
        model = payload.get("model") or settings.get("model", "wan3.0-video")
        assets = payload.get("assets") or []

        self._stage(job, "校验参数", 5)
        media_types = [a.get("media_type", "") for a in assets]
        combo_errors = rules.validate_combo(media_types)
        if combo_errors:
            raise RuntimeError("；".join(combo_errors))

        input_video_seconds = sum(
            float(a.get("duration") or 0)
            for a in assets
            if a.get("media_type") == "reference_video"
        )
        duration = int(payload.get("duration", 5))
        duration_errors = rules.validate_duration(
            duration, input_video_seconds, smart_value=DURATION_SMART
        )
        if duration_errors:
            raise RuntimeError("；".join(duration_errors))

        # 元数据骨架：先把「提交时的意图」记下来，后面再补「实际结果」
        meta: dict[str, Any] = {
            "job_id": job.id,
            "created_at": job.created_at,
            "model": model,
            "base_url": base_url,
            "region": settings.get("region"),
            "prompt": {
                "original": payload.get("original_prompt", ""),
                "optimized": payload.get("optimized_prompt", ""),
                "final": payload.get("prompt", ""),
                "optimize_enabled": bool(payload.get("optimize_enabled")),
                "prompt_extend": bool(payload.get("prompt_extend", True)),
            },
            "parameters": {
                "resolution": payload.get("resolution"),
                "ratio": payload.get("ratio"),
                "duration": duration,
                "duration_mode": "smart" if duration == DURATION_SMART else "custom",
                "audio": bool(payload.get("audio", True)),
                "seed": payload.get("seed", -1),
                "prompt_extend": bool(payload.get("prompt_extend", True)),
                "watermark": bool(payload.get("watermark", False)),
            },
            "assets": [],
            "mode": rules.derive_mode(media_types),
            "status": "running",
        }
        job.meta = meta

        # 参考素材：上传 + 复制本地副本
        self._stage(job, "处理参考素材", 15)
        media_payload: list[dict[str, Any]] = []

        for index, asset in enumerate(assets, start=1):
            if job.cancel_requested:
                raise RuntimeError("任务已被取消")

            entry: dict[str, Any] = {
                "index": index,
                "type": asset.get("media_type"),
                "name": asset.get("name"),
                "kind": asset.get("kind"),
                # 时长要记下来：复用参数时要靠它算参考视频的计费秒数
                "duration": asset.get("duration") or 0,
                "note": asset.get("note", ""),
            }

            local_path = asset.get("path")
            public_url = (asset.get("url") or "").strip()

            if local_path:
                source = Path(local_path)
                entry["local_source"] = str(source)

                if settings.get("keep_reference_copies", True):
                    copied = store.copy_reference(job.job_dir, source)
                    if copied:
                        entry["local_copy"] = copied

                cache_key = self._upload_cache_key(model, source)
                if cache_key in self._upload_cache:
                    oss_url = self._upload_cache[cache_key]
                    job.note(f"复用已上传的素材：{source.name}")
                else:
                    result = uploader.upload_file(base_url, api_key, model, source)
                    oss_url = result["oss_url"]
                    self._upload_cache[cache_key] = oss_url
                    entry["upload"] = {
                        "expires_note": result["note"],
                        "expire_in_seconds": result.get("expire_in_seconds"),
                    }
                    job.note(f"已上传素材：{source.name}")

                entry["oss_url"] = oss_url
                media_payload.append({"type": entry["type"], "url": oss_url})
            elif public_url:
                entry["public_url"] = public_url
                media_payload.append({"type": entry["type"], "url": public_url})
            else:
                raise RuntimeError(f"第 {index} 个素材既没有本地文件也没有 URL")

            meta["assets"].append(entry)

        # 组装参数
        parameters: dict[str, Any] = {
            "resolution": payload.get("resolution", "1080P"),
            "ratio": payload.get("ratio", "adaptive"),
            "duration": duration,
            "audio": bool(payload.get("audio", True)),
            "prompt_extend": bool(payload.get("prompt_extend", True)),
            # 水印默认关闭：官方文档写明 watermark 默认值就是 false。
            # 这里显式传，避免将来官方改默认值时行为跟着变。
            "watermark": bool(payload.get("watermark", False)),
        }

        seed = payload.get("seed")
        if seed not in (None, "", -1, "-1"):
            try:
                parameters["seed"] = int(seed)
            except (TypeError, ValueError):
                raise RuntimeError(f"seed 必须是整数，收到：{seed!r}")

        request_body = {
            "model": model,
            "input": {
                "prompt": payload.get("prompt", ""),
                **({"media": media_payload} if media_payload else {}),
            },
            "parameters": parameters,
        }

        store.write_request(job.job_dir, request_body)
        store.write_meta(job.job_dir, meta)

        # 创建任务
        self._stage(job, "提交生成任务", 30)
        created = dashscope.create_task(
            base_url,
            api_key,
            model=model,
            prompt=payload.get("prompt", ""),
            media=media_payload,
            parameters=parameters,
        )
        task_id = created["task_id"]
        meta["task_id"] = task_id
        meta["request_id"] = created.get("request_id")
        # 提交工单时对方第一个要的就是这个，所以单独留一份清单
        meta["request_ids"] = {"创建任务": created.get("request_id")}
        job.note(f"任务已创建：{task_id}")
        store.write_meta(job.job_dir, meta)

        # 轮询
        self._stage(job, "生成中（通常 1~5 分钟）", 40)
        poll_interval = int(settings.get("poll_interval_seconds", 15))
        ticks = {"count": 0}

        def on_tick(result: dict[str, Any]) -> None:
            ticks["count"] += 1
            job.progress = min(88, 40 + ticks["count"] * 3)
            status = result.get("task_status")
            job.step = f"生成中 · 第 {ticks['count']} 次查询 · {status}"

        finished = dashscope.poll_until_done(
            base_url,
            api_key,
            task_id,
            interval=poll_interval,
            on_tick=on_tick,
            should_stop=lambda: job.cancel_requested,
        )

        output = finished.get("output") or {}
        usage = finished.get("usage") or {}
        meta["task_status"] = finished.get("task_status")
        meta["usage"] = usage
        meta["submit_time"] = output.get("submit_time")
        meta["end_time"] = output.get("end_time")
        meta["orig_prompt"] = output.get("orig_prompt")
        meta["upstream_request_id"] = finished.get("request_id")
        meta.setdefault("request_ids", {})["查询任务"] = finished.get("request_id")

        if finished.get("task_status") != "SUCCEEDED":
            meta["status"] = "failed"
            meta["error"] = output.get("message") or output.get("code") or "任务未成功"
            meta["error_code"] = output.get("code")
            store.write_meta(job.job_dir, meta)
            raise RuntimeError(
                f"生成失败（{finished.get('task_status')}）："
                f"{output.get('message') or output.get('code') or '未返回原因'}"
            )

        video_url = output.get("video_url")
        if not video_url:
            raise RuntimeError("任务显示成功但没返回 video_url")

        # 立刻下载 —— URL 只有 24 小时
        self._stage(job, "下载视频到本地", 90)
        dest = job.job_dir / "video.mp4"
        size = dashscope.download_video(
            video_url, dest, retries=int(settings.get("download_retries", 3))
        )
        job.note(f"已下载：video.mp4（{size / 1024 / 1024:.1f} MB）")

        meta["video"] = {
            "file": "video.mp4",
            "size_bytes": size,
            "source_url": video_url,
            "source_url_note": "官方结果 URL 仅保留 24 小时，本地已保存副本。",
        }

        # 实际成本
        try:
            meta["cost"] = pricing.actual_cost(settings, model, usage)
        except Exception as exc:  # noqa: BLE001 - 成本算不出来不该让任务失败
            meta["cost"] = {"error": str(exc)}

        meta["status"] = "succeeded"
        job.status = "succeeded"
        job.step = "完成"
        job.progress = 100
        job.note("任务完成")
        store.write_meta(job.job_dir, meta)
        self._persist(job)

    # ---------- 内部工具 ----------

    def _stage(self, job: Job, step: str, progress: int) -> None:
        job.step = step
        job.progress = progress
        job.status = "running"
        job.note(step)

    def _persist(self, job: Job) -> None:
        """把最终状态写进 meta.json，让历史列表读得到。"""
        meta = job.meta or {}
        meta["status"] = job.status
        if job.error:
            meta["error"] = job.error
        meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
        store.write_meta(job.job_dir, meta)
        job.meta = meta

    def _upload_cache_key(self, model: str, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return f"{model}:{digest.hexdigest()}"
