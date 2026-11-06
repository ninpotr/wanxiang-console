"""音色档案、参考音频提取、台词合成任务落盘。

和 core/jobs.py 的区别：这里不管视频任务，只管「声音」页自己的数据。
音色档案存 outputs/voice/profiles.json，合成任务一个任务一个文件夹。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
import wave
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import config, store, voice_service

VOICE_DIR_NAME = "voice"
PROFILES_FILE = "profiles.json"
REFERENCE_DIR = "refs"
MAX_SOURCE_MB = 200
MIN_REFERENCE_SECONDS = 1.0
RECOMMENDED_REFERENCE_SECONDS = 3.0
# CosyVoice3 的 speech tokenizer 只接受 30 秒以内的参考音频
MAX_REFERENCE_SECONDS = 30.0
# 多段参考之间留的静音
REFERENCE_GAP_SECONDS = 0.25
# 生成结果短于这个值就视为失败（zero_shot 台词不匹配时会出现 0.0x 秒废片）
MIN_OUTPUT_SECONDS = 0.5
# seed=0 时按这个顺序给候选，都是本机实测过、音色比较稳的种子
DEFAULT_SEEDS = [4321, 1234, 5678]

TTS_MODEL = "Fun-CosyVoice3-0.5B"

EMOTIONS: list[dict[str, Any]] = [
    {
        "key": "angry",
        "label": "生气（保留音色）",
        "instruct": (
            "You are a helpful assistant. 请用非常生气、被东西砸到后恼怒质问的语气说一句话，"
            "语速稍慢，重音落在关键词上，保持少女音色。<|endofprompt|>"
        ),
        "speed": 0.95,
    },
    {
        "key": "threat",
        "label": "威胁/质问",
        "instruct": (
            "You are a helpful assistant. 请用非常生气、质问并带威胁感的语气说一句话，"
            "语速稍慢，重音落在关键词上，保持少女音色。<|endofprompt|>"
        ),
        "speed": 0.95,
    },
    {
        "key": "annoyed",
        "label": "不耐烦",
        "instruct": "You are a helpful assistant. 请用不耐烦、质问的语气说一句话，保持少女音色。<|endofprompt|>",
        "speed": 0.9,
    },
    {
        "key": "calm",
        "label": "平静",
        "instruct": "You are a helpful assistant. 请平静地说一句话。<|endofprompt|>",
        "speed": 1.0,
    },
    {
        "key": "happy",
        "label": "开心",
        "instruct": "You are a helpful assistant. 请非常开心地说一句话。<|endofprompt|>",
        "speed": 1.0,
    },
    {
        "key": "sad",
        "label": "伤心",
        "instruct": "You are a helpful assistant. 请非常伤心地说一句话。<|endofprompt|>",
        "speed": 0.95,
    },
    {
        "key": "whisper",
        "label": "小声",
        "instruct": "You are a helpful assistant. Please say a sentence in a very soft voice.<|endofprompt|>",
        "speed": 0.95,
    },
    {
        "key": "loud",
        "label": "大声",
        "instruct": "You are a helpful assistant. Please say a sentence as loudly as possible.<|endofprompt|>",
        "speed": 1.0,
    },
    {
        "key": "fast",
        "label": "快速",
        "instruct": "You are a helpful assistant. 请用尽可能快地语速说一句话。<|endofprompt|>",
        "speed": 1.2,
    },
    {
        "key": "slow",
        "label": "慢速",
        "instruct": "You are a helpful assistant. 请用尽可能慢地语速说一句话。<|endofprompt|>",
        "speed": 0.85,
    },
    {
        "key": "custom",
        "label": "自定义",
        "instruct": "",
        "speed": 1.0,
    },
]

_ASR_LOCK = threading.Lock()


class VoiceError(RuntimeError):
    pass


# ---------------------------------------------------------------- 路径


def voice_root(settings: dict[str, Any]) -> Path:
    root = store.output_root(settings) / VOICE_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def profiles_path(settings: dict[str, Any]) -> Path:
    return voice_root(settings) / PROFILES_FILE


def _safe_child(parent: Path, name: str) -> Path:
    base = parent.resolve()
    target = (base / name).resolve()
    if target != base and base not in target.parents:
        raise VoiceError("非法路径")
    return target


# ---------------------------------------------------------------- 档案


def load_profiles(settings: dict[str, Any]) -> list[dict[str, Any]]:
    path = profiles_path(settings)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def save_profiles(settings: dict[str, Any], profiles: list[dict[str, Any]]) -> None:
    path = profiles_path(settings)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def find_profile(settings: dict[str, Any], profile_id: str) -> dict[str, Any]:
    for profile in load_profiles(settings):
        if profile.get("id") == profile_id:
            return profile
    raise VoiceError(f"音色不存在：{profile_id}")


def reference_path(settings: dict[str, Any], profile: dict[str, Any]) -> Path:
    raw = profile.get("reference_rel") or ""
    path = (config.ROOT / raw).resolve()
    if not path.exists():
        raise VoiceError(f"参考音频不见了：{path}")
    return path


def create_profile(
    settings: dict[str, Any],
    *,
    name: str,
    prompt_text: str,
    notes: str,
    source: Path | None = None,
    sources: list[Path] | None = None,
) -> dict[str, Any]:
    """建音色档案。sources 可以多段，按顺序拼接；source 保留给旧调用。"""
    name = (name or "").strip()
    if not name:
        raise VoiceError("音色名称不能为空")
    if len(name) > 40:
        raise VoiceError("音色名称最多 40 个字")

    uploads = [Path(item) for item in (sources or [])]
    if source is not None:
        uploads.insert(0, Path(source))
    if not uploads:
        raise VoiceError("至少上传一段参考音频或视频")
    if len(uploads) > 10:
        raise VoiceError("一次最多上传 10 段参考音频")

    for item in uploads:
        if not item.exists():
            raise VoiceError(f"上传文件不存在：{item}")
        if item.stat().st_size > MAX_SOURCE_MB * 1024 * 1024:
            raise VoiceError(f"上传文件超过 {MAX_SOURCE_MB}MB，请先裁剪参考音频")

    profile_id = uuid.uuid4().hex[:12]
    folder = voice_root(settings) / REFERENCE_DIR / profile_id
    folder.mkdir(parents=True, exist_ok=True)

    segment_paths: list[Path] = []
    source_names: list[str] = []
    segment_durations: list[float] = []
    try:
        for index, item in enumerate(uploads, 1):
            suffix = item.suffix.lower() or ".bin"
            source_copy = folder / f"source_{index}{suffix}"
            shutil.copy2(item, source_copy)
            segment = folder / f"segment_{index}.wav"
            _extract_audio(source_copy, segment)
            segment_paths.append(segment)
            source_names.append(item.name)
            segment_durations.append(round(wav_duration(segment), 3))

        reference = folder / "reference.wav"
        if len(segment_paths) == 1:
            shutil.copy2(segment_paths[0], reference)
        else:
            _concat_wavs(segment_paths, reference, gap_seconds=REFERENCE_GAP_SECONDS)
        duration = wav_duration(reference)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    warnings: list[str] = []
    if duration < MIN_REFERENCE_SECONDS:
        shutil.rmtree(folder, ignore_errors=True)
        raise VoiceError(f"参考音频只有 {duration:.2f} 秒，太短，至少要 {MIN_REFERENCE_SECONDS:.0f} 秒")
    if duration < RECOMMENDED_REFERENCE_SECONDS:
        warnings.append(
            f"参考音频只有 {duration:.2f} 秒，短于建议的 {RECOMMENDED_REFERENCE_SECONDS:.0f} 秒，"
            "音色相似度可能不稳定。"
        )
    if duration > MAX_REFERENCE_SECONDS:
        shutil.rmtree(folder, ignore_errors=True)
        raise VoiceError(
            f"参考音频合计 {duration:.2f} 秒，超过本机模型支持的 {MAX_REFERENCE_SECONDS:.0f} 秒，请裁剪后再传。"
        )

    def rel(path: Path) -> str:
        try:
            return str(path.relative_to(config.ROOT))
        except ValueError:
            # output_dir 被设置到项目目录外时，存绝对路径
            return str(path.resolve())

    reference_rel = rel(reference)
    now = datetime.now().isoformat(timespec="seconds")
    profile = {
        "id": profile_id,
        "name": name,
        "prompt_text": (prompt_text or "").strip(),
        "notes": (notes or "").strip(),
        "reference_rel": reference_rel,
        "source_name": "、".join(source_names),
        "source_names": source_names,
        "source_count": len(segment_paths),
        "segments": [rel(path) for path in segment_paths],
        "segment_durations": segment_durations,
        "duration": round(duration, 3),
        "warnings": warnings,
        "created_at": now,
        "updated_at": now,
    }
    profiles = load_profiles(settings)
    profiles.append(profile)
    save_profiles(settings, profiles)
    return profile


def update_profile(
    settings: dict[str, Any],
    profile_id: str,
    *,
    name: str | None = None,
    prompt_text: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    profiles = load_profiles(settings)
    for profile in profiles:
        if profile.get("id") != profile_id:
            continue
        if name is not None:
            name = name.strip()
            if not name:
                raise VoiceError("音色名称不能为空")
            if len(name) > 40:
                raise VoiceError("音色名称最多 40 个字")
            profile["name"] = name
        if prompt_text is not None:
            profile["prompt_text"] = prompt_text.strip()
        if notes is not None:
            profile["notes"] = notes.strip()
        profile["updated_at"] = datetime.now().isoformat(timespec="seconds")
        save_profiles(settings, profiles)
        return profile
    raise VoiceError(f"音色不存在：{profile_id}")


def delete_profile(settings: dict[str, Any], profile_id: str) -> None:
    profiles = load_profiles(settings)
    remaining = [p for p in profiles if p.get("id") != profile_id]
    if len(remaining) == len(profiles):
        raise VoiceError(f"音色不存在：{profile_id}")
    save_profiles(settings, remaining)
    folder = voice_root(settings) / REFERENCE_DIR / profile_id
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------- ffmpeg


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise VoiceError("找不到 ffmpeg，无法处理音频。请安装 ffmpeg 并确保它在 PATH 中。")
    return path


def _gpl_ffmpeg() -> str | None:
    bundled = (
        config.ROOT
        / "tools/bin/ffmpeg/ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe"
    )
    return str(bundled) if bundled.exists() else None


def _extract_audio(source: Path, target: Path) -> None:
    ffmpeg = _ffmpeg()
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
        str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    if proc.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        raise VoiceError(
            "从上传文件里抽音频失败。如果上传的是视频，请确认它包含音轨。\n"
            f"ffmpeg：{proc.stderr[-500:]}"
        )


def _concat_wavs(paths: list[Path], target: Path, *, gap_seconds: float = REFERENCE_GAP_SECONDS) -> None:
    """把多段 24k 单声道 WAV 按顺序拼起来，段间留静音。"""
    if not paths:
        raise VoiceError("没有可拼接的参考音频")
    with wave.open(str(paths[0]), "rb") as first:
        params = first.getparams()
    with wave.open(str(target), "wb") as out:
        out.setnchannels(params.nchannels)
        out.setsampwidth(params.sampwidth)
        out.setframerate(params.framerate)
        silence = b"\x00" * int(params.framerate * gap_seconds) * params.nchannels * params.sampwidth
        for index, path in enumerate(paths):
            with wave.open(str(path), "rb") as source:
                if (
                    source.getnchannels() != params.nchannels
                    or source.getsampwidth() != params.sampwidth
                    or source.getframerate() != params.framerate
                ):
                    raise VoiceError("参考音频的采样格式不一致，无法拼接")
                out.writeframes(source.readframes(source.getnframes()))
            if index < len(paths) - 1:
                out.writeframes(silence)


def _to_mp3(wav: Path) -> Path | None:
    ffmpeg = _gpl_ffmpeg() or _ffmpeg()
    mp3 = wav.with_suffix(".mp3")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "192k", str(mp3),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if proc.returncode != 0 or not mp3.exists():
        mp3.unlink(missing_ok=True)
        return None
    return mp3


def wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            if rate:
                return frames / float(rate)
    except (wave.Error, OSError):
        pass
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise VoiceError("读取音频时长失败：既不是可读 WAV，也找不到 ffprobe。")
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise VoiceError(f"读取音频时长失败：{proc.stderr[-300:]}") from exc


# ---------------------------------------------------------------- ASR


def transcribe_profile(settings: dict[str, Any], profile_id: str) -> str:
    """用百炼 Paraformer 实时接口识别参考音频，返回参考台词。"""
    profile = find_profile(settings, profile_id)
    path = reference_path(settings, profile)
    api_key = (settings.get("api_key") or "").strip()
    if not api_key:
        raise VoiceError("还没填写 API Key，无法自动识别。请先在设置页填写。")

    try:
        import dashscope
        from dashscope.audio.asr.recognition import Recognition, RecognitionCallback
    except ImportError as exc:
        raise VoiceError("缺少 dashscope 依赖，请运行 pip install -r requirements.txt") from exc

    with _ASR_LOCK:
        dashscope.api_key = api_key
        try:
            recognition = Recognition(
                model="paraformer-realtime-v2",
                callback=RecognitionCallback(),
                format="wav",
                sample_rate=24000,
            )
            result = recognition.call(str(path))
        except Exception as exc:
            raise VoiceError(f"自动识别失败：{type(exc).__name__}: {exc}") from exc

    if getattr(result, "status_code", 0) != 200:
        raise VoiceError(
            f"自动识别失败：HTTP {getattr(result, 'status_code', '?')} "
            f"{getattr(result, 'code', '')} {getattr(result, 'message', '')}"
        )
    output = getattr(result, "output", {}) or {}
    sentences = output.get("sentence") or []
    text = "".join(str(item.get("text") or "") for item in sentences).strip()
    if not text:
        raise VoiceError("自动识别没有返回文字，请手动填写参考台词。")
    return text


# ---------------------------------------------------------------- TTS 任务


def create_tts_job(
    settings: dict[str, Any],
    *,
    profile_id: str,
    text: str,
    mode: str,
    instruct: str,
    speed: float,
    seed: int,
    variants: int,
) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise VoiceError("台词不能为空")
    if len(text) > 500:
        raise VoiceError("单次台词最多 500 字")
    if mode not in ("instruct", "zero_shot"):
        raise VoiceError("mode 只能是 instruct 或 zero_shot")
    variants = max(1, min(3, int(variants or 1)))
    speed = max(0.5, min(2.0, float(speed or 1.0)))
    seed = max(0, int(seed or 0))

    profile = find_profile(settings, profile_id)
    ref = reference_path(settings, profile)
    if mode == "zero_shot" and not (profile.get("prompt_text") or "").strip():
        raise VoiceError("原声复刻模式需要参考台词，请先编辑音色补上参考台词。")

    root = voice_root(settings)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    job_dir = root / f"{stamp}_{store.slugify(text)}"
    index = 2
    while job_dir.exists():
        job_dir = root / f"{stamp}_{store.slugify(text)}_{index}"
        index += 1
    job_dir.mkdir(parents=True)

    refs_dir = job_dir / "refs"
    refs_dir.mkdir()
    shutil.copy2(ref, refs_dir / "reference.wav")

    try:
        requests_payload: list[dict[str, Any]] = []
        variants_meta: list[dict[str, Any]] = []
        for i in range(variants):
            if seed:
                variant_seed = seed + i * 1000
            else:
                variant_seed = DEFAULT_SEEDS[i % len(DEFAULT_SEEDS)]
            audio_path = job_dir / f"audio_{i + 1}.wav"
            payload = {
                "text": text,
                "mode": mode,
                "ref_audio": str(ref.resolve()),
                "prompt_text": profile.get("prompt_text", ""),
                "instruct": instruct,
                "speed": speed,
                "seed": variant_seed,
                "out_path": str(audio_path.resolve()),
            }
            requests_payload.append(payload)
            result = voice_service.synthesize(settings, payload)
            if not audio_path.exists():
                raise VoiceError(f"本机语音服务没有生成文件：{audio_path}")
            actual_duration = float(result.get("duration") or wav_duration(audio_path))
            if actual_duration < MIN_OUTPUT_SECONDS:
                raise VoiceError(
                    f"第 {i + 1} 个候选只有 {actual_duration:.2f} 秒，像是生成失败。"
                    "原声复刻模式常见原因是参考台词和参考音频不匹配；"
                    "请改用情绪指令模式，或把参考台词按每段参考音频逐段填写。"
                )
            mp3 = _to_mp3(audio_path)
            variants_meta.append(
                {
                    "index": i + 1,
                    "wav": audio_path.name,
                    "mp3": mp3.name if mp3 else None,
                    "duration": round(actual_duration, 3),
                    "sample_rate": result.get("sample_rate"),
                    "seed": result.get("seed"),
                    "elapsed": result.get("elapsed"),
                }
            )

        now = datetime.now().isoformat(timespec="seconds")
        meta = {
            "id": job_dir.name,
            "created_at": now,
            "text": text,
            "mode": mode,
            "instruct": instruct,
            "speed": speed,
            "seed": seed,
            "model": TTS_MODEL,
            "profile": {
                "id": profile.get("id"),
                "name": profile.get("name"),
                "duration": profile.get("duration"),
            },
            "variants": variants_meta,
        }
        store.write_json(job_dir / "meta.json", meta)
        store.write_json(job_dir / "request.json", requests_payload)
        return job_to_dict(job_dir, meta)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


def job_to_dict(job_dir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    job_id = job_dir.name
    variants = []
    for item in meta.get("variants") or []:
        wav = item.get("wav")
        mp3 = item.get("mp3")
        variants.append(
            {
                **item,
                "wav_url": f"/api/voice/audio/{quote(job_id)}/{quote(wav)}" if wav else None,
                "mp3_url": f"/api/voice/audio/{quote(job_id)}/{quote(mp3)}" if mp3 else None,
            }
        )
    return {
        "id": job_id,
        "created_at": meta.get("created_at"),
        "text": meta.get("text"),
        "mode": meta.get("mode"),
        "instruct": meta.get("instruct"),
        "speed": meta.get("speed"),
        "seed": meta.get("seed"),
        "model": meta.get("model"),
        "profile": meta.get("profile"),
        "variants": variants,
        "dir": str(job_dir),
    }


def list_history(settings: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
    root = voice_root(settings)
    jobs: list[dict[str, Any]] = []
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name == REFERENCE_DIR:
            continue
        meta_path = entry / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        jobs.append(job_to_dict(entry, meta))
    jobs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return jobs[:limit]


def find_job_dir(settings: dict[str, Any], job_id: str) -> Path:
    root = voice_root(settings)
    target = _safe_child(root, job_id)
    if not target.is_dir() or target.name == REFERENCE_DIR:
        raise VoiceError(f"合成任务不存在：{job_id}")
    return target


def job_audio_path(settings: dict[str, Any], job_id: str, filename: str) -> Path:
    if Path(filename).suffix.lower() not in (".wav", ".mp3"):
        raise VoiceError("只允许读取 wav/mp3")
    job_dir = find_job_dir(settings, job_id)
    target = _safe_child(job_dir, filename)
    if not target.exists():
        raise VoiceError(f"音频文件不存在：{filename}")
    return target


def delete_job(settings: dict[str, Any], job_id: str) -> None:
    job_dir = find_job_dir(settings, job_id)
    shutil.rmtree(job_dir, ignore_errors=True)


def reveal(path: Path, settings: dict[str, Any] | None = None) -> None:
    target = Path(path).resolve()
    roots = [(config.ROOT / "outputs").resolve()]
    if settings is not None:
        roots.append(store.output_root(settings).resolve())
    if not any(target == root or root in target.parents for root in roots):
        raise VoiceError("只能打开 outputs 目录下的文件")
    if target.is_file():
        target = target.parent
    if not target.exists():
        raise VoiceError(f"目录不存在：{target}")
    if os.name == "nt":
        os.startfile(str(target))  # type: ignore[attr-defined]
    else:
        subprocess.run(["open" if os.uname().sysname == "Darwin" else "xdg-open", str(target)], check=False)
