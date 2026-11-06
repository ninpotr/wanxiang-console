"""把本地素材传到百炼的临时存储，换回 oss:// 临时 URL。

官方接口分两步（见「上传本地文件获取临时URL」）：
  1. GET  {base}/uploads?action=getPolicy&model={model}   拿上传凭证
  2. POST {policy.upload_host}                            表单上传文件

三个容易踩的点，这里都处理掉了：
  - 上传时必须指定 model，且**必须与后续调用生成时的 model 完全一致**，
    所以调用方的 model 参数一路透传到这里，不允许各写各的。
  - 临时 URL 有效期 48 小时，且上传后不可查询、不可下载，
    所以要落本地副本（见 store.py）。
  - 使用 oss:// 调用生成接口时必须带 X-DashScope-OssResourceResolve 头，
    漏了的报错信息完全不提 header，极难排查 —— 见 dashscope.py。
"""

from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path
from typing import Any

import requests

from . import logging_setup

logger = logging_setup.get_logger()

UPLOAD_POLICY_TIMEOUT = 30
UPLOAD_TIMEOUT = 600
ALLOWED_TEMPORARY_HOST_SUFFIX = ".aliyuncs.com"


class UploadError(RuntimeError):
    """上传失败。message 直接展示给用户，所以要说人话。"""


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _safe_object_name(path: Path) -> str:
    """把文件名转成 OSS 安全的 ASCII 名，保留扩展名。

    原名带中文/空格时，multipart 表单里的 key 容易出现编码歧义；
    这里统一转写，反正这个 key 只用于本次调用。
    """
    stem = unicodedata.normalize("NFKD", path.stem)
    stem = stem.encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if not stem:
        stem = "file"
    return f"{stem[:60]}{path.suffix.lower()}"


def get_upload_policy(base_url: str, api_key: str, model: str) -> dict[str, Any]:
    """步骤 1：拿上传凭证。"""
    url = f"{base_url.rstrip('/')}/uploads"
    start = time.perf_counter()
    try:
        resp = requests.get(
            url,
            headers=_headers(api_key),
            params={"action": "getPolicy", "model": model},
            timeout=UPLOAD_POLICY_TIMEOUT,
        )
    except requests.RequestException as exc:
        logging_setup.log_http(
            logger, "GET(上传凭证)", url, None,
            elapsed=time.perf_counter() - start, error=str(exc),
        )
        raise UploadError(f"获取上传凭证失败（网络错误）：{exc}") from exc

    logging_setup.log_http(
        logger, "GET(上传凭证)", url, resp.status_code,
        elapsed=time.perf_counter() - start,
        error=None if resp.status_code == 200 else resp.text[:200],
        body=f"model={model}",
    )

    if resp.status_code == 429:
        raise UploadError(
            "获取上传凭证被限流（该接口限制 100 QPS）。稍后重试即可。"
        )
    if resp.status_code == 401:
        raise UploadError(
            "获取上传凭证失败：API Key 无效，或 Key 的地域与当前选择的接口地址不一致。"
        )
    if resp.status_code != 200:
        raise UploadError(f"获取上传凭证失败（HTTP {resp.status_code}）：{resp.text[:300]}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise UploadError(f"获取上传凭证失败：返回不是 JSON：{resp.text[:300]}") from exc

    policy = payload.get("data")
    if not isinstance(policy, dict):
        message = payload.get("message") or payload.get("code") or str(payload)[:300]
        raise UploadError(f"获取上传凭证失败：{message}")

    required = ("upload_host", "upload_dir", "oss_access_key_id", "policy", "signature")
    missing = [key for key in required if not policy.get(key)]
    if missing:
        raise UploadError(f"上传凭证字段缺失：{', '.join(missing)}")

    # 凭证里的 upload_host 会随请求一起发送文件，确认它确实是阿里云域名。
    host = str(policy["upload_host"])
    if not host.startswith("https://") or ALLOWED_TEMPORARY_HOST_SUFFIX not in host:
        raise UploadError(f"上传凭证返回了非预期的上传地址，已中止：{host}")

    return policy


def upload_file(
    base_url: str,
    api_key: str,
    model: str,
    file_path: Path,
    *,
    retry_on_conflict: bool = True,
) -> dict[str, Any]:
    """步骤 2：上传文件，返回 oss:// 临时 URL 及元信息。"""
    file_path = Path(file_path)
    if not file_path.exists():
        raise UploadError(f"文件不存在：{file_path}")

    object_name = _safe_object_name(file_path)
    policy = get_upload_policy(base_url, api_key, model)
    key = f"{policy['upload_dir']}/{object_name}"

    form: dict[str, Any] = {
        "OSSAccessKeyId": (None, policy["oss_access_key_id"]),
        "Signature": (None, policy["signature"]),
        "policy": (None, policy["policy"]),
        "x-oss-object-acl": (None, policy.get("x_oss_object_acl", "private")),
        "x-oss-forbid-overwrite": (None, policy.get("x_oss_forbid_overwrite", "true")),
        "key": (None, key),
        "success_action_status": (None, "200"),
    }

    start = time.perf_counter()
    try:
        with file_path.open("rb") as handle:
            # file 必须是最后一个表单域，所以在这里才插入
            form["file"] = (object_name, handle)
            resp = requests.post(
                policy["upload_host"], files=form, timeout=UPLOAD_TIMEOUT
            )
    except requests.RequestException as exc:
        logging_setup.log_http(
            logger, "POST(上传文件)", policy["upload_host"], None,
            elapsed=time.perf_counter() - start, error=str(exc),
        )
        raise UploadError(f"上传失败（网络错误）：{exc}") from exc

    logging_setup.log_http(
        logger, "POST(上传文件)", policy["upload_host"], resp.status_code,
        elapsed=time.perf_counter() - start,
        error=None if resp.status_code == 200 else resp.text[:200],
        body=f"{object_name}",
    )

    if resp.status_code != 200:
        # x-oss-forbid-overwrite=true，同名文件会 409；换一份凭证拿新的 upload_dir 再试一次。
        if retry_on_conflict and resp.status_code in (409, 403):
            return upload_file(
                base_url, api_key, model, file_path, retry_on_conflict=False
            )
        raise UploadError(
            f"上传失败（HTTP {resp.status_code}）：{resp.text[:300]}"
        )

    return {
        "oss_url": f"oss://{key}",
        "object_name": object_name,
        "upload_dir": policy["upload_dir"],
        "expire_in_seconds": policy.get("expire_in_seconds"),
        "max_file_size_mb": policy.get("max_file_size_mb"),
        "note": "临时 URL 有效期 48 小时，且上传后不可查询、不可下载。",
    }
