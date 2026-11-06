"""万相3.0 视频生成 API 客户端。

官方是异步两步：创建任务拿 task_id -> 轮询拿结果。
task_id 与结果视频 URL 都只有 24 小时有效期。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import requests

from . import logging_setup

logger = logging_setup.get_logger()

CREATE_TIMEOUT = 60
QUERY_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 900
DOWNLOAD_CHUNK = 1024 * 512

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}


class ApiError(RuntimeError):
    """接口返回的错误。message 直接展示给用户。"""


def _headers(api_key: str, *, async_call: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if async_call:
        # 官方：视频生成 HTTP 只支持异步，缺这个头会报
        # "current user api does not support synchronous calls"
        headers["X-DashScope-Async"] = "enable"
    # 只要请求里可能出现 oss:// 临时 URL 就必须带这个头。
    # 无条件带上：它无害，而漏掉它的报错信息（"The media format is not
    # supported or incorrect for the data inspection"）完全不提 header，
    # 是这套 API 里最难排查的一个坑。
    headers["X-DashScope-OssResourceResolve"] = "enable"
    return headers


# 官方错误码 -> 人话。官方返回的 message 多数是英文，直接甩给用户等于没说。
# 文案来源：官方错误码文档 raw/model-api-reference/preparations/error-code.md
ERROR_HINTS: dict[str, str] = {
    "Arrearage": (
        "阿里云账号欠费了，百炼把调用全停了。\n"
        "去「阿里云控制台 → 费用与成本 → 充值」，充完等几分钟再试。\n"
        "（失败的任务不计费，这一条没有扣钱）"
    ),
    "InvalidApiKey": "API Key 无效，或 Key 的地域与设置里选的地域不一致。",
    "Throttling": "触发限流了，等几十秒再试。",
    "Throttling.RateQuota": "请求太频繁，等几十秒再试。",
    "DataInspectionFailed": (
        "输入内容没通过安全审核。检查提示词里有没有敏感词，"
        "或者参考素材有没有问题。"
    ),
    "InvalidParameter": "参数不合法，看下面的详细信息。",
    "Model.AccessDenied": (
        "没有这个模型的调用权限。去百炼控制台的「业务空间 → 模型列表」"
        "打开对应模型的「模型调用」开关，或改用主账号默认业务空间的 API Key。"
    ),
    "Workspace.AccessDenied": "无权访问该业务空间，确认 API Key 和业务空间是否匹配。",
    "AllocationQuota.FreeTierOnly": (
        "免费额度用完了，而且开了「免费额度用完即停」。"
        "去控制台关掉这个开关，或者充值。"
    ),
    "InvalidFile": "素材文件不合法，检查格式、大小和时长是否符合要求。",
    "DownloadFileFailed": "素材下载失败，确认 URL 是可公开访问的。",
}


def _explain_http_error(status: int, payload: Any, raw: str) -> str:
    code = ""
    message = ""
    if isinstance(payload, dict):
        code = str(payload.get("code") or "")
        message = str(payload.get("message") or "")
    detail = message or raw[:300]

    # 前缀匹配：官方有 Throttling.RateQuota 这种带子类的码
    for key, hint in ERROR_HINTS.items():
        if code == key or code.startswith(key + "."):
            return f"{hint}\n错误码：{code}\n官方原文：{detail}"

    if status == 401:
        return f"API Key 无效，或 Key 的地域与当前选择的接口地址不一致。\n官方原文：{detail}"
    if status == 429:
        return f"触发限流，稍后重试。\n官方原文：{detail}"
    if status == 403:
        return f"没有权限或凭证过期。\n官方原文：{detail}"
    return f"HTTP {status}：{detail}"


def test_connection(base_url: str, api_key: str) -> dict[str, Any]:
    """测连通性：故意查一个不存在的 task_id。

    Key 有效时服务端会正常回应「查不到这个任务」；Key 无效或地域不匹配才返回 401。
    这样不用真的花钱生成一条视频就能验证配置。
    """
    url = f"{base_url.rstrip('/')}/tasks/00000000-0000-0000-0000-000000000000"
    start = time.perf_counter()
    try:
        resp = requests.get(url, headers=_headers(api_key), timeout=QUERY_TIMEOUT)
    except requests.RequestException as exc:
        logging_setup.log_http(
            logger, "GET(测试连接)", url, None,
            elapsed=time.perf_counter() - start, error=str(exc),
        )
        return {"ok": False, "message": f"网络不通：{exc}"}

    elapsed = time.perf_counter() - start
    try:
        payload = resp.json()
    except ValueError:
        payload = None

    request_id = payload.get("request_id") if isinstance(payload, dict) else None
    logging_setup.log_http(
        logger, "GET(测试连接)", url, resp.status_code,
        request_id=request_id, elapsed=elapsed,
        error=None if resp.status_code == 200 else (payload or {}).get("message") if isinstance(payload, dict) else resp.text[:200],
    )

    if resp.status_code == 200:
        return {
            "ok": True,
            "message": "连接正常，API Key 可用。（查了个不存在的任务，返回 200 是预期行为）",
        }
    if resp.status_code == 401 or (isinstance(payload, dict) and payload.get("code") == "InvalidApiKey"):
        return {
            "ok": False,
            "message": (
                "API Key 无效，或 Key 的地域与所选地域不一致。"
                "各地域的 Key 相互独立、不能跨地域使用，请先确认 Key 是哪个地域的。"
            ),
        }
    # 其它错误（例如查无此任务的业务错误）说明鉴权已经过了，连接是通的。
    return {
        "ok": True,
        "message": f"接口可达（HTTP {resp.status_code}），鉴权已通过。",
        "detail": _explain_http_error(resp.status_code, payload, resp.text),
    }


def list_tasks(
    base_url: str,
    api_key: str,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
    model_name: str | None = None,
    status: str | None = None,
    page_no: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """批量查询异步任务。

    官方原文：能查到「当前 API Key 所属阿里云主账号下的所有任务
    （包括该主账号下通过任意 API Key 提交的任务）」—— 也就是说，
    不管任务是用控制台、这个工具还是别的什么提交的，只要还在保留期内都能看到。

    两个硬限制（官方的，不是实现限制）：
      - start_time 和 end_time 的跨度不能超过 24 小时；都不传则查最近 24 小时
      - 已结束的任务超时后会被系统清理，查不到就是永久没了

    时间格式：YYYYMMDDhhmmss。
    """
    url = f"{base_url.rstrip('/')}/tasks"
    params: dict[str, Any] = {"page_no": page_no, "page_size": page_size}
    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time
    if model_name:
        params["model_name"] = model_name
    if status:
        params["status"] = status

    start = time.perf_counter()
    try:
        resp = requests.get(
            url, headers=_headers(api_key), params=params, timeout=QUERY_TIMEOUT
        )
    except requests.RequestException as exc:
        logging_setup.log_http(
            logger, "GET(任务列表)", url, None,
            elapsed=time.perf_counter() - start, error=str(exc),
        )
        raise ApiError(f"查询任务列表失败（网络错误）：{exc}") from exc

    try:
        payload = resp.json()
    except ValueError:
        raise ApiError(f"查询任务列表失败：返回不是 JSON：{resp.text[:300]}")

    logging_setup.log_http(
        logger, "GET(任务列表)", url, resp.status_code,
        request_id=payload.get("request_id"), elapsed=time.perf_counter() - start,
        error=None if resp.status_code == 200 else resp.text[:250],
        body=f"page={page_no} 共{payload.get('total')}条",
    )

    if resp.status_code != 200:
        raise ApiError(_explain_http_error(resp.status_code, payload, resp.text))

    return payload


def create_task(
    base_url: str,
    api_key: str,
    *,
    model: str,
    prompt: str,
    media: list[dict[str, Any]],
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """创建生成任务，返回 {task_id, request_id, raw}。"""
    url = f"{base_url.rstrip('/')}/services/aigc/video-generation/video-synthesis"

    body: dict[str, Any] = {"model": model}
    input_payload: dict[str, Any] = {}
    if prompt:
        input_payload["prompt"] = prompt
    if media:
        input_payload["media"] = media
    body["input"] = input_payload
    if parameters:
        body["parameters"] = parameters

    start = time.perf_counter()
    try:
        resp = requests.post(
            url, headers=_headers(api_key, async_call=True), json=body, timeout=CREATE_TIMEOUT
        )
    except requests.RequestException as exc:
        logging_setup.log_http(
            logger, "POST(创建任务)", url, None,
            elapsed=time.perf_counter() - start, error=f"网络错误：{exc}",
        )
        raise ApiError(f"创建任务失败（网络错误）：{exc}") from exc

    elapsed = time.perf_counter() - start
    try:
        payload = resp.json()
    except ValueError:
        logging_setup.log_http(
            logger, "POST(创建任务)", url, resp.status_code,
            elapsed=elapsed, error="返回不是 JSON", body=resp.text[:300],
        )
        raise ApiError(f"创建任务失败：返回不是 JSON：{resp.text[:300]}")

    request_id = payload.get("request_id")
    if resp.status_code != 200:
        logging_setup.log_http(
            logger, "POST(创建任务)", url, resp.status_code,
            request_id=request_id, elapsed=elapsed,
            error=payload.get("message") or payload.get("code"), body=resp.text[:500],
        )
        raise ApiError(_explain_http_error(resp.status_code, payload, resp.text))

    output = payload.get("output") or {}
    task_id = output.get("task_id")
    logging_setup.log_http(
        logger, "POST(创建任务)", url, resp.status_code,
        request_id=request_id, elapsed=elapsed, task_id=task_id,
    )

    if not task_id:
        raise ApiError(
            f"创建任务失败：响应里没有 task_id。"
            f"{payload.get('message') or str(payload)[:300]}"
        )

    return {
        "task_id": task_id,
        "task_status": output.get("task_status"),
        "request_id": payload.get("request_id"),
        "raw": payload,
    }


def query_task(base_url: str, api_key: str, task_id: str) -> dict[str, Any]:
    """查询任务状态。返回 {task_status, output, usage, request_id, raw}。"""
    url = f"{base_url.rstrip('/')}/tasks/{task_id}"
    try:
        resp = requests.get(url, headers=_headers(api_key), timeout=QUERY_TIMEOUT)
    except requests.RequestException as exc:
        logging_setup.log_http(logger, "GET(查询任务)", url, None, task_id=task_id, error=str(exc))
        raise ApiError(f"查询任务失败（网络错误）：{exc}") from exc

    try:
        payload = resp.json()
    except ValueError:
        logging_setup.log_http(
            logger, "GET(查询任务)", url, resp.status_code, task_id=task_id,
            error="返回不是 JSON", body=resp.text[:300],
        )
        raise ApiError(f"查询任务失败：返回不是 JSON：{resp.text[:300]}")

    if resp.status_code != 200:
        # 轮询期间只在出错时记，否则每 15 秒一行会把日志刷满
        logging_setup.log_http(
            logger, "GET(查询任务)", url, resp.status_code, task_id=task_id,
            request_id=payload.get("request_id"),
            error=payload.get("message") or payload.get("code"), body=resp.text[:500],
        )
        raise ApiError(_explain_http_error(resp.status_code, payload, resp.text))

    output = payload.get("output") or {}
    return {
        "task_status": output.get("task_status"),
        "output": output,
        "usage": payload.get("usage") or {},
        "request_id": payload.get("request_id"),
        "raw": payload,
    }


def poll_until_done(
    base_url: str,
    api_key: str,
    task_id: str,
    *,
    interval: int = 15,
    on_tick: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """轮询到终态。

    官方建议 15 秒一次；查询接口默认 RPS 20，别打太密。
    """
    while True:
        if should_stop is not None and should_stop():
            raise ApiError("任务已被取消")

        result = query_task(base_url, api_key, task_id)
        if on_tick is not None:
            on_tick(result)

        if result["task_status"] in TERMINAL_STATUSES:
            output = result.get("output") or {}
            logging_setup.log_http(
                logger, "GET(查询任务·终态)", f"/tasks/{task_id}", 200,
                task_id=task_id,
                request_id=result.get("request_id"),
                error=(
                    None if result["task_status"] == "SUCCEEDED"
                    else f"{result['task_status']}: {output.get('code')} {output.get('message')}"
                ),
            )
            return result

        # 睡满间隔，但每秒检查一次停止信号，避免取消操作要等 15 秒才生效。
        for _ in range(max(1, interval)):
            if should_stop is not None and should_stop():
                raise ApiError("任务已被取消")
            time.sleep(1)


def download_video(url: str, dest: Path, retries: int = 3) -> int:
    """下载视频到本地。结果 URL 只有 24 小时，必须落到本地才算完成。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        start = time.perf_counter()
        try:
            with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as resp:
                if resp.status_code != 200:
                    logging_setup.log_http(
                        logger, "GET(下载视频)", f"/download/{dest.name}", resp.status_code,
                        error=f"第 {attempt} 次尝试失败",
                    )
                    raise ApiError(f"下载失败（HTTP {resp.status_code}）")
                tmp = dest.with_suffix(dest.suffix + ".part")
                written = 0
                with tmp.open("wb") as handle:
                    for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK):
                        if chunk:
                            handle.write(chunk)
                            written += len(chunk)
                tmp.replace(dest)
                logging_setup.log_http(
                    logger, "GET(下载视频)", f"/download/{dest.name}", 200,
                    elapsed=time.perf_counter() - start,
                    body=f"{written / 1024 / 1024:.1f} MB",
                )
                return written
        except (requests.RequestException, ApiError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)

    raise ApiError(f"下载失败，已重试 {retries} 次：{last_error}")
