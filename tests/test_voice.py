import json
import math
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
from core import config, voice

HAS_FFMPEG = bool(shutil.which("ffmpeg"))


def write_sine(path: Path, seconds: float = 2.0, sample_rate: int = 24000, freq: float = 220.0) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        frames = bytearray()
        for index in range(int(seconds * sample_rate)):
            value = int(0.2 * 32767 * math.sin(2 * math.pi * freq * index / sample_rate))
            frames += struct.pack("<h", value)
        handle.writeframes(bytes(frames))


class VoiceProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="voice_test_", dir=config.ROOT))
        self.settings = {"output_dir": str(self.temp), "api_key": "test-key"}
        self.source = self.temp / "source.wav"
        write_sine(self.source)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg is required")
    def test_create_update_and_delete_profile(self) -> None:
        profile = voice.create_profile(
            self.settings,
            name="示例音色",
            prompt_text="这是示例参考台词。",
            notes="测试",
            source=self.source,
        )

        self.assertEqual(profile["name"], "示例音色")
        self.assertGreaterEqual(profile["duration"], 1.9)
        reference = voice.reference_path(self.settings, profile)
        self.assertTrue(reference.exists())
        self.assertEqual(reference.suffix, ".wav")

        loaded = voice.load_profiles(self.settings)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["id"], profile["id"])

        updated = voice.update_profile(
            self.settings, profile["id"], name="示例音色2", prompt_text="新台词"
        )
        self.assertEqual(updated["name"], "示例音色2")
        self.assertEqual(updated["prompt_text"], "新台词")

        voice.delete_profile(self.settings, profile["id"])
        self.assertEqual(voice.load_profiles(self.settings), [])
        self.assertFalse(reference.parent.exists())

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg is required")
    def test_tts_job_writes_audio_meta_and_history(self) -> None:
        profile = voice.create_profile(
            self.settings,
            name="示例音色",
            prompt_text="这是示例参考台词。",
            notes="",
            source=self.source,
        )

        def fake_synthesize(settings, payload, **_kwargs):
            path = Path(payload["out_path"])
            write_sine(path, seconds=1.0)
            return {
                "duration": 1.0,
                "seed": payload.get("seed") or 1,
                "sample_rate": 24000,
                "elapsed": 0.01,
            }

        with patch.object(voice.voice_service, "synthesize", side_effect=fake_synthesize), \
             patch.object(voice, "_to_mp3", return_value=None):
            job = voice.create_tts_job(
                self.settings,
                profile_id=profile["id"],
                text="可恶，啥情况啊！",
                mode="instruct",
                instruct="You are a helpful assistant. 请非常生气地说一句话。<|endofprompt|>",
                speed=1.0,
                seed=0,
                variants=2,
            )

        self.assertEqual(len(job["variants"]), 2)
        job_dir = Path(job["dir"])
        self.assertTrue((job_dir / "audio_1.wav").exists())
        self.assertTrue((job_dir / "audio_2.wav").exists())
        self.assertTrue((job_dir / "meta.json").exists())
        self.assertTrue((job_dir / "request.json").exists())
        self.assertTrue((job_dir / "refs" / "reference.wav").exists())
        self.assertEqual(job["profile"]["name"], "示例音色")

        history = voice.list_history(self.settings)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], job["id"])

        path = voice.job_audio_path(self.settings, job["id"], "audio_1.wav")
        self.assertTrue(path.exists())
        with self.assertRaises(voice.VoiceError):
            voice.job_audio_path(self.settings, job["id"], "../audio_1.wav")
        with self.assertRaises(voice.VoiceError):
            voice.job_audio_path(self.settings, job["id"], "profiles.json")

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg is required")
    def test_multi_reference_profile_concatenates_in_order(self) -> None:
        second = self.temp / "second.wav"
        write_sine(second, seconds=1.5, freq=330.0)
        profile = voice.create_profile(
            self.settings,
            name="多段示例",
            prompt_text="第一段\n第二段",
            notes="",
            sources=[self.source, second],
        )

        self.assertEqual(profile["source_count"], 2)
        self.assertEqual(profile["source_names"], ["source.wav", "second.wav"])
        self.assertAlmostEqual(profile["duration"], 2.0 + 1.5 + 0.25, delta=0.08)
        self.assertEqual(len(profile["segments"]), 2)
        self.assertTrue(voice.reference_path(self.settings, profile).exists())

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg is required")
    def test_default_seed_set_uses_curated_seeds(self) -> None:
        profile = voice.create_profile(
            self.settings,
            name="示例音色",
            prompt_text="这是示例参考台词。",
            notes="",
            source=self.source,
        )
        seen: list[int] = []

        def fake_synthesize(settings, payload, **_kwargs):
            seen.append(payload["seed"])
            write_sine(Path(payload["out_path"]), seconds=1.0)
            return {
                "duration": 1.0,
                "seed": payload["seed"],
                "sample_rate": 24000,
                "elapsed": 0.01,
            }

        with patch.object(voice.voice_service, "synthesize", side_effect=fake_synthesize), \
             patch.object(voice, "_to_mp3", return_value=None):
            job = voice.create_tts_job(
                self.settings,
                profile_id=profile["id"],
                text="可恶，啥情况啊！",
                mode="instruct",
                instruct="You are a helpful assistant.<|endofprompt|>",
                speed=0.95,
                seed=0,
                variants=3,
            )

        self.assertEqual(seen, [4321, 1234, 5678])
        self.assertEqual([item["seed"] for item in job["variants"]], [4321, 1234, 5678])

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg is required")
    def test_short_output_is_rejected_and_job_removed(self) -> None:
        profile = voice.create_profile(
            self.settings,
            name="示例音色",
            prompt_text="这是示例参考台词。",
            notes="",
            source=self.source,
        )

        def fake_synthesize(settings, payload, **_kwargs):
            write_sine(Path(payload["out_path"]), seconds=0.1)
            return {
                "duration": 0.1,
                "seed": payload["seed"],
                "sample_rate": 24000,
                "elapsed": 0.01,
            }

        with patch.object(voice.voice_service, "synthesize", side_effect=fake_synthesize), \
             patch.object(voice, "_to_mp3", return_value=None):
            with self.assertRaises(voice.VoiceError):
                voice.create_tts_job(
                    self.settings,
                    profile_id=profile["id"],
                    text="是谁啊？",
                    mode="zero_shot",
                    instruct="",
                    speed=1.0,
                    seed=0,
                    variants=1,
                )

        self.assertEqual(voice.list_history(self.settings), [])


class VoiceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="voice_api_test_", dir=config.ROOT))
        self.settings = {"output_dir": str(self.temp), "api_key": "test-key"}

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_options_and_empty_profiles(self) -> None:
        with patch.object(app_module.config, "load_settings", return_value=self.settings):
            with TestClient(app_module.app) as client:
                options = client.get("/api/voice/options")
                profiles = client.get("/api/voice/profiles")

        self.assertEqual(options.status_code, 200)
        self.assertTrue(any(item["key"] == "angry" for item in options.json()["emotions"]))
        self.assertEqual(profiles.status_code, 200)
        self.assertEqual(profiles.json()["profiles"], [])

    def test_status_reports_service_not_running(self) -> None:
        fake_status = {"running": False, "error": "connection refused"}
        with patch.object(app_module.config, "load_settings", return_value=self.settings), \
             patch.object(app_module.voice_service, "status", return_value=fake_status):
            with TestClient(app_module.app) as client:
                response = client.get("/api/voice/status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["running"])

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg is required")
    def test_upload_profile_and_tts(self) -> None:
        source = self.temp / "ref.wav"
        write_sine(source, seconds=2.0)
        files = {"file": ("ref.wav", source.read_bytes(), "audio/wav")}
        data = {"name": "示例音色", "prompt_text": "这是示例参考台词。", "notes": ""}

        def fake_synthesize(settings, payload, **_kwargs):
            path = Path(payload["out_path"])
            write_sine(path, seconds=1.0)
            return {"duration": 1.0, "seed": 1, "sample_rate": 24000, "elapsed": 0.01}

        with patch.object(app_module.config, "load_settings", return_value=self.settings), \
             patch.object(voice.voice_service, "synthesize", side_effect=fake_synthesize), \
             patch.object(voice, "_to_mp3", return_value=None):
            with TestClient(app_module.app) as client:
                created = client.post("/api/voice/profiles", files=files, data=data)
                self.assertEqual(created.status_code, 200, created.text)
                profile = created.json()["profile"]

                job = client.post(
                    "/api/voice/tts",
                    json={
                        "profile_id": profile["id"],
                        "text": "牛马的谁？",
                        "mode": "instruct",
                        "instruct": "You are a helpful assistant. 请非常生气地说一句话。<|endofprompt|>",
                        "speed": 1.0,
                        "seed": 0,
                        "variants": 1,
                    },
                )
                self.assertEqual(job.status_code, 200, job.text)
                job_data = job.json()["job"]
                self.assertEqual(job_data["text"], "牛马的谁？")
                self.assertEqual(len(job_data["variants"]), 1)

                audio = client.get(job_data["variants"][0]["wav_url"])
                self.assertEqual(audio.status_code, 200)
                self.assertEqual(audio.headers["content-type"], "audio/wav")

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg is required")
    def test_upload_multiple_references(self) -> None:
        first = self.temp / "first.wav"
        second = self.temp / "second.wav"
        write_sine(first, seconds=1.5)
        write_sine(second, seconds=1.5, freq=330.0)
        files = [
            ("files", ("first.wav", first.read_bytes(), "audio/wav")),
            ("files", ("second.wav", second.read_bytes(), "audio/wav")),
        ]
        with patch.object(app_module.config, "load_settings", return_value=self.settings):
            with TestClient(app_module.app) as client:
                response = client.post(
                    "/api/voice/profiles",
                    files=files,
                    data={"name": "多段示例", "prompt_text": "第一段\n第二段"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        profile = response.json()["profile"]
        self.assertEqual(profile["source_count"], 2)
        self.assertEqual(profile["source_names"], ["first.wav", "second.wav"])

    def test_tts_with_unknown_profile_returns_400(self) -> None:
        with patch.object(app_module.config, "load_settings", return_value=self.settings):
            with TestClient(app_module.app) as client:
                response = client.post(
                    "/api/voice/tts",
                    json={
                        "profile_id": "does-not-exist",
                        "text": "测试",
                        "mode": "instruct",
                        "instruct": "You are a helpful assistant.<|endofprompt|>",
                    },
                )
        self.assertEqual(response.status_code, 400)
        self.assertIn("音色不存在", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
