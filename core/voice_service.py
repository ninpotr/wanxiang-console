"""本机 CosyVoice3 服务的进程管理与 HTTP 客户端。

控制台自己不加载模型：模型跑在独立的 Python 3.11 venv 里，通过
127.0.0.1:9880 通信。这样主服务的 Python 3.13 环境不会被 torch 依赖污染，
也能在空闲时把显存释放掉。

设计原则：失败就原样报错，不偷偷换云端 TTS。
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import requests

from . import config, store

_lock = threading.Lock()
_process: subprocess.Popen | None = None
_started_by_us = False

LOG_DIR = config.ROOT / "logs"
PID_FILE = LOG_DIR / "tts_server.pid"
LOG_FILE = LOG_DIR / "tts_server.log"


class VoiceServiceError(RuntimeError):
    pass


def _cfg(settings: dict[str, Any]) -> dict[str, Any]:
    return settings.get("voice") or {}


def service_url(settings: dict[str, Any]) -> str:
    return (_cfg(settings).get("service_url") or "http://127.0.0.1:9880").rstrip("/")


def _request(
    settings: dict[str, Any],
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    url = service_url(settings) + path
    try:
        response = requests.request(method, url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise VoiceServiceError(f"连不上本机语音服务 {url}：{exc}") from exc

    if response.status_code >= 400:
        detail = response.text
        try:
            body = response.json()
            detail = body.get("detail") or body.get("message") or detail
        except ValueError:
            pass
        raise VoiceServiceError(f"本机语音服务返回 {response.status_code}：{detail}")
    try:
        return response.json()
    except ValueError as exc:
        raise VoiceServiceError(f"本机语音服务返回了非 JSON 内容：{response.text[:300]}") from exc


def status(settings: dict[str, Any], *, timeout: float = 2.0) -> dict[str, Any]:
    """返回服务状态。服务没起来不算错误，直接返回 running=False。"""
    cfg = _cfg(settings)
    base = {
        "running": False,
        "model_loaded": False,
        "loading": False,
        "service_url": service_url(settings),
        "service_python": cfg.get("service_python", ""),
        "service_script": cfg.get("service_script", ""),
        "model_dir": cfg.get("model_dir", ""),
        "idle_seconds": cfg.get("idle_unload_seconds", 600),
        "error": None,
    }
    try:
        data = _request(settings, "GET", "/health", timeout=timeout)
    except VoiceServiceError as exc:
        base["error"] = str(exc)
        return base
    base.update(data)
    base["running"] = True
    return base


def start(settings: dict[str, Any], *, wait_seconds: float = 180.0) -> dict[str, Any]:
    """启动本机服务并等到 HTTP 端口可用。不预加载模型。"""
    global _process, _started_by_us

    with _lock:
        current = status(settings)
        if current.get("running"):
            return current

        cfg = _cfg(settings)
        if cfg.get("enabled") is False:
            raise VoiceServiceError("本机语音服务在设置里被关闭了（voice.enabled=false）。")

        python = Path(cfg.get("service_python") or "")
        script = Path(cfg.get("service_script") or "")
        model_dir = Path(cfg.get("model_dir") or "")
        if not python.exists():
            raise VoiceServiceError(
                f"找不到本机语音服务的 Python：{python}\n"
                "请按 SETUP.md 配置本地语音服务，或在设置里修改 voice.service_python。"
            )
        if not script.exists():
            raise VoiceServiceError(
                f"找不到本机语音服务脚本：{script}\n"
                "请按 SETUP.md 配置本地语音服务，或在设置里修改 voice.service_script。"
            )

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        voice_root = store.output_root(settings) / "voice"
        staging_root = config.ROOT / "_staging"
        voice_root.mkdir(parents=True, exist_ok=True)
        staging_root.mkdir(exist_ok=True)

        port = service_url(settings).rsplit(":", 1)[-1].split("/")[0] or "9880"
        cmd = [
            str(python),
            str(script),
            "--port", port,
            "--model-dir", str(model_dir),
            "--allowed-root", str(voice_root),
            "--allowed-root", str(staging_root),
            "--idle-seconds", str(int(cfg.get("idle_unload_seconds", 600))),
        ]

        log_handle = LOG_FILE.open("a", encoding="utf-8", errors="replace")
        log_handle.write(f"\n\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
        log_handle.flush()
        creationflags = 0
        if platform.system() == "Windows":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            _process = subprocess.Popen(
                cmd,
                cwd=str(script.parent),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except OSError as exc:
            log_handle.close()
            raise VoiceServiceError(f"启动本机语音服务失败：{exc}") from exc

        PID_FILE.write_text(str(_process.pid), encoding="ascii")
        _started_by_us = True

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            time.sleep(1.0)
            if _process.poll() is not None:
                log_handle.close()
                tail = _tail_log()
                raise VoiceServiceError(
                    f"本机语音服务启动后退出（退出码 {_process.returncode}）。\n"
                    f"日志最后几行：\n{tail}"
                )
            state = status(settings, timeout=2.0)
            if state.get("running"):
                return state

        log_handle.close()
        raise VoiceServiceError(
            f"等了 {int(wait_seconds)} 秒，本机语音服务还没起来。\n日志最后几行：\n{_tail_log()}"
        )


def stop(settings: dict[str, Any]) -> dict[str, Any]:
    """释放模型并尽量停掉由控制台启动的进程。"""
    global _process, _started_by_us

    try:
        _request(settings, "POST", "/unload", timeout=5.0)
    except VoiceServiceError:
        pass

    with _lock:
        if _process is not None and _process.poll() is None:
            _process.terminate()
            try:
                _process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _process.kill()
        _process = None
        _started_by_us = False

    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            pid = 0
        if pid:
            _kill_pid(pid)
        try:
            PID_FILE.unlink()
        except OSError:
            pass
    return status(settings)


def load_model(settings: dict[str, Any]) -> dict[str, Any]:
    state = status(settings)
    if not state.get("running"):
        state = start(settings)
    return _request(settings, "POST", "/load", timeout=300.0)


def unload_model(settings: dict[str, Any]) -> dict[str, Any]:
    state = status(settings)
    if not state.get("running"):
        return state
    return _request(settings, "POST", "/unload", timeout=10.0)


def synthesize(settings: dict[str, Any], payload: dict[str, Any], *, timeout: float = 600.0) -> dict[str, Any]:
    """调用本机服务合成一段音频。首次会触发模型加载，所以超时给得比较长。"""
    state = status(settings)
    if not state.get("running"):
        cfg = _cfg(settings)
        if cfg.get("auto_start", True):
            start(settings)
        else:
            raise VoiceServiceError(
                "本机语音服务没有启动。请先在「声音」页点「启动服务」，或在设置里打开 voice.auto_start。"
            )
    return _request(settings, "POST", "/tts", payload, timeout=timeout)


def _tail_log(lines: int = 40) -> str:
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "（日志不可读）"
    return "\n".join(content[-lines:]) or "（日志为空）"


def _kill_pid(pid: int) -> None:
    if platform.system() == "Windows":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def _cleanup() -> None:
    if not _started_by_us:
        return
    try:
        stop(config.load_settings())
    except Exception:
        pass


atexit.register(_cleanup)
