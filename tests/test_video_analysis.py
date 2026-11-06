import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

import app as app_module

from core.video_analysis import (
    VideoAnalysisError,
    VideoBaseline,
    analyze,
    _extract_audio_rms,
    _format_timestamp,
    _measure_dark_edges,
    _parse_result,
    _rounded_views,
    _sample_times,
    _summarize_black_bands,
)


class VideoAnalysisTests(unittest.TestCase):
    def test_upload_endpoint_accepts_a_valid_baseline_without_external_model_call(self):
        with TestClient(app_module.app) as client:
            with patch("app._run_video_analysis", return_value={"accepted": True}) as run:
                response = client.post(
                    "/api/video-analysis",
                    files={"file": ("sample.mp4", b"local-test-video", "video/mp4")},
                    data={"baseline": json.dumps({"avg_views": 1500})},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": True})
        run.assert_called_once()
        self.assertEqual(run.call_args.args[1], "sample.mp4")
        self.assertEqual(run.call_args.args[2].avg_views, 1500)
        self.assertFalse(run.call_args.args[3])

    def test_upload_endpoint_forwards_explicit_audio_metrics_choice(self):
        with TestClient(app_module.app) as client:
            with patch("app._run_video_analysis", return_value={"accepted": True}) as run:
                response = client.post(
                    "/api/video-analysis",
                    files={"file": ("sample.mp4", b"local-test-video", "video/mp4")},
                    data={
                        "baseline": json.dumps({"avg_views": 1500}),
                        "include_audio_metrics": "true",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(run.call_args.args[3])

    def test_sample_times_cover_video_without_sampling_past_end(self):
        times = _sample_times(12)

        self.assertEqual(len(times), 16)
        self.assertEqual(times[:7], [0, 0.5, 1, 1.5, 2, 2.5, 3])
        self.assertAlmostEqual(times[-1], 11.95)
        self.assertEqual(len(_sample_times(20)), 24)
        self.assertEqual(len(_sample_times(90)), 40)
        self.assertEqual(_sample_times(0.5), [0.25])
        self.assertEqual(_format_timestamp(61.25), "01:01.25")

    def test_detects_symmetric_dark_bands_but_not_a_dark_frame(self):
        pixels = bytearray([255] * (64 * 64))
        for row in range(8):
            pixels[row * 64:(row + 1) * 64] = bytes(64)
            start = (63 - row) * 64
            pixels[start:start + 64] = bytes(64)

        result = _measure_dark_edges(bytes(pixels))

        self.assertEqual(result, {
            "orientation": "top_bottom",
            "border_area_percent": 25.0,
        })
        self.assertIsNone(_measure_dark_edges(bytes(64 * 64))["orientation"])

    def test_one_frame_is_not_enough_to_confirm_suspected_black_bands(self):
        result = _summarize_black_bands([
            (0.0, Path("frame.jpg"), {
                "orientation": "top_bottom",
                "border_area_percent": 25.0,
            }),
        ])

        self.assertEqual(result["status"], "not_consistent")
        self.assertEqual(result["frame_count"], 1)
        self.assertEqual(result["timestamps"], ["00:00.00"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_extracts_per_second_audio_rms_values_without_uploading_audio(self):
        ffmpeg = shutil.which("ffmpeg")
        with tempfile.TemporaryDirectory() as temp:
            audio_path = Path(temp) / "tone.wav"
            subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i",
                    "sine=frequency=1000:sample_rate=48000:duration=3",
                    "-c:a", "pcm_s16le", str(audio_path),
                ],
                check=True,
                capture_output=True,
            )

            result = _extract_audio_rms(
                audio_path, 3, Path(temp), has_audio=True,
            )

        self.assertEqual(result["status"], "analyzed")
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual([sample["second"] for sample in result["samples"]], [0, 1, 2])
        self.assertTrue(all(-23 <= sample["rms_dbfs"] <= -19 for sample in result["samples"]))

    def test_baseline_requires_positive_view_count_and_bounded_rates(self):
        with self.assertRaises(ValidationError):
            VideoBaseline(avg_views=0)
        with self.assertRaises(ValidationError):
            VideoBaseline(avg_views=1000, completion_rate=101)

    def test_forecast_rounds_views_to_readable_values(self):
        self.assertEqual(_rounded_views(127), 130)
        self.assertEqual(_rounded_views(1_250), 1_300)
        self.assertEqual(_rounded_views(12_500), 13_000)

    def test_rejects_misordered_forecast(self):
        payload = {
            "score": 70,
            "headline": "开头可以更直接",
            "summary": "基于抽样画面。",
            "strengths": ["主体清楚"],
            "timeline": [{
                "time": "00:00.0",
                "visual": "主体在画面中央",
                "role": "hook",
                "decision": "keep",
                "reason": "可见内容清楚",
            }],
            "issues": [],
            "actions": [{"priority": 1, "title": "改开头", "steps": "先展示结果", "expected_effect": "增强停留"}],
            "revised_prompt": "拍摄一段短视频。",
            "forecast": {
                "low_multiplier": 1.2,
                "likely_multiplier": 1.0,
                "high_multiplier": 1.4,
                "confidence": "低",
                "assumptions": "只是情景估计。",
            },
        }

        with self.assertRaises(VideoAnalysisError):
            _parse_result(json.dumps(payload, ensure_ascii=False))

    def test_normalizes_action_steps_returned_as_a_string_list(self):
        payload = {
            "score": 70,
            "headline": "开场可以更直接",
            "summary": "基于抽样画面。",
            "strengths": ["主体清楚"],
            "timeline": [{
                "time": "00:00.0",
                "visual": "主体在画面中央",
                "role": "hook",
                "decision": "keep",
                "reason": "可见内容清楚",
            }],
            "issues": [],
            "actions": [{
                "priority": 1,
                "title": "改开头",
                "steps": ["先展示结果", "删除重复铺垫"],
                "expected_effect": "增强停留",
            }],
            "revised_prompt": "拍摄一段短视频。",
            "forecast": {
                "low_multiplier": 0.8,
                "likely_multiplier": 1.0,
                "high_multiplier": 1.2,
                "confidence": "低",
                "assumptions": "只是情景估计。",
            },
        }

        result = _parse_result(json.dumps(payload, ensure_ascii=False))

        self.assertEqual(result.actions[0].steps, "先展示结果\n删除重复铺垫")

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg and ffprobe are required",
    )
    def test_analyzes_sampled_frames_and_uses_baseline_for_forecast(self):
        ffmpeg = shutil.which("ffmpeg")
        with tempfile.TemporaryDirectory() as temp:
            video_path = Path(temp) / "sample.mp4"
            subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=white:s=320x180:r=24",
                    "-f", "lavfi", "-i",
                    "sine=frequency=1000:sample_rate=48000:duration=16",
                    "-vf", "pad=320:240:0:30:black",
                    "-t", "16", "-c:v", "mpeg4", "-c:a", "aac",
                    "-pix_fmt", "yuv420p",
                    str(video_path),
                ],
                check=True,
                capture_output=True,
            )
            model_response = {
                "score": 75,
                "headline": "主体清楚，开头可以更直接",
                "summary": "依据抽取画面判断，不包含音频分析。",
                "strengths": ["主体明确"],
                "timeline": [
                    {
                        "time": "00:00.0",
                        "visual": f"第 {index + 1} 张抽样画面",
                        "role": "setup",
                        "decision": "keep",
                        "reason": "画面信息明确。",
                    }
                    for index, _ in enumerate(_sample_times(16))
                ],
                "issues": [],
                "actions": [
                    {
                        "priority": 1,
                        "title": "强化开头",
                        "steps": "开头先展示核心画面。",
                        "expected_effect": "争取提高前段停留。",
                    }
                ],
                "revised_prompt": "生成主体明确的短视频。",
                "forecast": {
                    "low_multiplier": 0.8,
                    "likely_multiplier": 1.0,
                    "high_multiplier": 1.2,
                    "confidence": "低",
                    "assumptions": "按近期账号平均播放量估算。",
                },
            }
            with patch(
                "core.video_analysis.prompt_opt.call_model",
                return_value=json.dumps(model_response, ensure_ascii=False),
            ) as call_model:
                result = analyze(
                    video_path,
                    video_name="sample.mp4",
                    baseline=VideoBaseline(avg_views=1_000),
                    model="test-vision",
                    base_url="https://example.invalid/v1",
                    api_key="test-key",
                    include_audio_metrics=True,
                )

        self.assertEqual(len(call_model.call_args.args[5]), 20)
        self.assertEqual(len(result["analysis"]["timeline"]), 20)
        self.assertEqual(result["analysis"]["timeline"][0]["time"], "00:00.00")
        self.assertEqual(result["analysis"]["timeline"][-1]["time"], "00:15.95")
        self.assertGreaterEqual(result["video_info"]["duration"], 15)
        self.assertEqual(result["video_info"]["black_bands"]["status"], "possible")
        self.assertEqual(result["video_info"]["black_bands"]["frame_count"], 20)
        self.assertIn("疑似上下黑边", call_model.call_args.args[4])
        self.assertAlmostEqual(
            result["video_info"]["black_bands"]["median_area_percent"], 25.0, delta=3.0,
        )
        self.assertEqual(result["video_info"]["audio_metrics"]["status"], "analyzed")
        self.assertEqual(result["video_info"]["audio_metrics"]["sample_count"], 16)
        self.assertIn("0s=", call_model.call_args.args[4])
        self.assertIn("音频文件未发送", call_model.call_args.args[4])
        self.assertEqual(result["analysis"]["forecast"]["views"], {
            "low": 800, "likely": 1_000, "high": 1_200, "baseline": 1_000,
        })


if __name__ == "__main__":
    unittest.main()
