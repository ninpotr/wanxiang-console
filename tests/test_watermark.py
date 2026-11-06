import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
from core import watermark


class FakePopen:
    """不启动真正的 GPU 推理，只模拟 runner 的进度和输出文件。"""

    def __init__(self, cmd, **kwargs):
        self.pid = 424242
        spec_path = Path(cmd[cmd.index("--spec") + 1])
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        output = Path(spec["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake-output")
        live = spec.get("live_preview")
        if live:
            Path(live).write_bytes(b"fake-preview")
        self.stdout = iter([
            "@@WM_PROGRESS " + json.dumps({
                "progress": 20, "stage": "检测", "boxes": [[1, 2, 3, 4]],
                "width": 1280, "height": 720,
            }, ensure_ascii=False) + "\n",
            "@@WM_PROGRESS " + json.dumps({
                "progress": 100, "stage": "完成",
            }, ensure_ascii=False) + "\n",
        ])

    def wait(self):
        return 0

    def poll(self):
        return 0


class WatermarkManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="wm_test_", dir=app_module.ROOT))
        self.jobs_dir = self.temp / "jobs"
        self.source = self.temp / "source.png"
        self.source.write_bytes(b"source")
        self.output_dir = self.temp / "outputs"
        # 单元测试用假 Popen，不需要真实的 VSR venv；把检查路径指向存在的文件。
        watermark.VSR_PYTHON = self.source
        watermark.WM_RUNNER = self.source
        self.manager = watermark.WatermarkManager(self.jobs_dir)

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def _wait(self, job, seconds=5):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if job.status in watermark.TERMINAL:
                return job
            time.sleep(0.05)
        self.fail(f"job did not finish: {job.status} {job.stage}")

    def test_submit_runs_runner_and_records_result(self):
        with patch("core.watermark.subprocess.Popen", FakePopen):
            job = self.manager.submit(
                self.source,
                source_type="staged",
                label="source.png",
                media_type="image",
                mode="lama-direct",
                areas=[[0.7, 0.8, 1.0, 1.0]],
                output_dir=self.output_dir,
            )
            self._wait(job)

        self.assertEqual(job.status, "done")
        self.assertEqual(job.progress, 100)
        self.assertTrue(job.output_path.exists())
        data = job.to_dict()
        self.assertTrue(data["has_file"])
        self.assertTrue(data["has_live_preview"])
        self.assertEqual(data["result"]["boxes"], [[1, 2, 3, 4]])
        self.assertEqual(data["result"]["width"], 1280)
        self.assertEqual(data["result"]["bytes"], len(b"fake-output"))

        # 任务记录和参数文件都落盘，记录删掉后两个文件都消失
        self.assertTrue(self.manager._path(job.id).exists())
        self.assertTrue(self.manager._spec_path(job.id).exists())
        self.assertTrue(self.manager.forget(job.id))
        self.assertFalse(self.manager._path(job.id).exists())
        self.assertFalse(self.manager._spec_path(job.id).exists())

    def test_load_old_job_json_without_mode(self):
        old_output = self.temp / "old.png"
        old_output.write_bytes(b"old")
        record = {
            "id": "old123",
            "source": str(self.source),
            "source_type": "staged",
            "source_label": "old.png",
            "media_type": "image",
            "engine": "lama",
            "output": str(old_output),
            "preview": "",
            "status": "done",
            "progress": 100,
            "stage": "完成",
        }
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        (self.jobs_dir / "old123.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )

        manager = watermark.WatermarkManager(self.jobs_dir)
        job = manager.get("old123")
        self.assertIsNotNone(job)
        self.assertEqual(job.mode, "lama")
        self.assertEqual(job.detect_model, "fast")
        self.assertEqual(job.media_type, "image")
        self.assertEqual(job.status, "done")


class WatermarkApiValidationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)
        self.staging = Path(tempfile.mkdtemp(prefix="wm_api_", dir=app_module.STAGING_DIR))
        self.image = self.staging / "frame.png"
        self.image.write_bytes(b"not-a-real-image")
        self.video = self.staging / "clip.mp4"
        self.video.write_bytes(b"not-a-real-video")

    def tearDown(self):
        shutil.rmtree(self.staging, ignore_errors=True)

    def _post(self, payload):
        return self.client.post("/api/watermark", json=payload)

    def test_rejects_direct_mode_without_areas(self):
        response = self._post({
            "source_type": "staged", "source": str(self.image),
            "mode": "lama-direct", "areas": [],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("框出", response.json()["detail"])

    def test_rejects_invalid_area(self):
        response = self._post({
            "source_type": "staged", "source": str(self.image),
            "mode": "lama-direct", "areas": [[0.9, 0.9, 0.1, 0.1]],
        })
        self.assertEqual(response.status_code, 400)

    def test_rejects_file_outside_staging(self):
        response = self._post({
            "source_type": "staged", "source": str(Path(app_module.ROOT) / "app.py"),
            "mode": "lama-direct", "areas": [[0.7, 0.8, 1, 1]],
        })
        self.assertEqual(response.status_code, 400)

    def test_auto_detect_can_skip_manual_areas_and_forces_image_lama(self):
        captured = {}

        class FakeJob:
            def to_dict(self):
                return {"id": "fake"}

        def fake_submit(*args, **kwargs):
            captured.update(kwargs)
            return FakeJob()

        with patch.object(app_module.watermark_manager, "submit", side_effect=fake_submit):
            response = self._post({
                "source_type": "staged", "source": str(self.image),
                "mode": "sttn-auto", "auto_detect": True,
                "areas": [[2, 2, 3, 3]],  # auto 模式下应被忽略，不校验
                "detect_model": "precise",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["mode"], "lama")
        self.assertEqual(captured["media_type"], "image")
        self.assertTrue(captured["auto_detect"])
        self.assertEqual(captured["detect_model"], "precise")

    def test_valid_video_request_passes_areas_and_mode(self):
        captured = {}

        class FakeJob:
            def to_dict(self):
                return {"id": "fake"}

        def fake_submit(*args, **kwargs):
            captured.update(kwargs)
            return FakeJob()

        with patch.object(app_module.watermark_manager, "submit", side_effect=fake_submit):
            response = self._post({
                "source_type": "staged", "source": str(self.video),
                "mode": "sttn-det", "areas": [[0.6, 0.8, 1, 1]],
                "detect_model": "fast", "ab": [0.5, 2.0],
                "advanced": {"sttnNeighborStride": 7, "not_a_key": 1},
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["mode"], "sttn-det")
        self.assertEqual(captured["media_type"], "video")
        self.assertEqual(captured["areas"], [[0.6, 0.8, 1, 1]])
        self.assertEqual(captured["ab"], [0.5, 2.0])
        self.assertEqual(captured["advanced"], {"sttnNeighborStride": 7})
