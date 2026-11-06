"""提示词整理层 —— 原样复制官方 wan3-pe Skill。

官方在《万相3.0-视频生成》的「提示词技巧」一节提供这个 Skill（wan3-pe.zip），
本文档原文已复制到 skills/wan3-pe/SKILL.md，这里只做加载和调用，不添加额外指令。

三个必须说清楚的事实：
  1. 这一层做的是「结构化整理」，不是「扩写」。Skill 自己写明：
     「只梳理用户已经写出的剧情、主体、台词与参考诉求；不替用户扩写剧情，
       不加入用户没提过的内容。」
  2. 真正的扩写由服务端的 prompt_extend 完成，而那个结果是黑盒 ——
     万相3.0 视频 API 的响应里只有 orig_prompt（原文），没有改写后的文本。
     （对比：图片模型会返回 actual_prompt。）
  3. 所以把这一层放到界面上、可预览可编辑，是唯一能抓住的控制点。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
from pathlib import Path
from typing import Any

import requests

from . import logging_setup
from .config import ROOT

logger = logging_setup.get_logger()

SKILL_PATH = ROOT / "skills" / "wan3-pe" / "SKILL.md"

REQUEST_TIMEOUT = 180
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MIME_FALLBACK = "image/png"


class PromptHelperError(RuntimeError):
    """提示词整理失败。message 直接展示给用户。"""


def explain_error(status: int, body: str, model: str, slot: str = "文本模型") -> str:
    """把 OpenAI 兼容接口的错误翻译成能照着做的话。

    slot 指明出问题的是设置里的哪一项（「文本模型」还是「视觉模型」）——
    不然用户会照着错的提示去改另一个设置，白折腾。
    """
    code = ""
    message = ""
    request_id = ""

    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        payload = None

    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            message = str(err.get("message") or "")
        elif isinstance(err, str):
            message = err
        elif payload.get("message"):
            message = str(payload["message"])
            code = str(payload.get("code") or "")
        request_id = str(payload.get("request_id") or payload.get("id") or "")

    if not message:
        message = body[:200]

    rid = f"\n（request_id：{request_id}）" if request_id else ""
    head = f"HTTP {status}"
    if code:
        head += f" {code}"

    # 欠费：官方用英文写"Access denied, please make sure your account is in
    # good standing"，光看这句完全猜不到是欠费。先于 403 判断。
    if code == "Arrearage" or "overdue-payment" in body or "good standing" in message:
        return (
            "阿里云账号欠费了，百炼把调用全停了。\n"
            "去「阿里云控制台 → 费用与成本 → 充值」，充完等几分钟再试。"
            f"{rid}"
        )

    # 401：Key 或地域问题
    if status == 401 or code in ("invalid_api_key", "InvalidApiKey"):
        return (
            f"{head}：API Key 无效，或 Key 的地域与所选地域不一致。\n"
            "各地域的 Key 相互独立、不能跨地域使用，请确认设置页的地域"
            f"和控制台右上角选的是同一个。{rid}"
        )

    # 403 access_denied / 404 model_not_found：这个模型用不了
    if status in (403, 404) or code in (
        "access_denied", "model_access_denied", "model_not_found", "model_not_exist"
    ):
        why = (
            "你的 API Key 来自某个业务空间，而该业务空间没有被授权调用这个模型"
            if status == 403 else
            "这个模型名在当前地域不存在，或你的业务空间看不到它"
        )
        return (
            f"{head}：{slot}「{model}」用不了。\n"
            f"原因：{why}。\n"
            "三条路，任选一条：\n"
            f"  1. 去百炼控制台的「业务空间管理」→ 你那个空间 → 「模型列表」页签，\n"
            f"     搜这个模型名，打开「模型授权」区域里「模型调用」列的开关\n"
            "  2. 改用主账号「默认业务空间」的 API Key ——\n"
            "     官方原文：「默认业务空间无法设置此限制，所有模型均可调用」\n"
            f"  3. 换一个你有权限的模型：在「设置 → {slot}」里改名字，\n"
            "     改完点旁边的「测试这个模型能不能用」确认，不用猜\n"
            "如果暂时不想处理，可以关掉「设置 → 提示词整理」，跳过这一步照样能生成视频。"
            f"{rid}"
        )

    return f"{head}：{message}{rid}"


def skill_available() -> bool:
    return SKILL_PATH.exists()


def test_model(
    settings: dict[str, Any], *, base_url: str, api_key: str, model: str,
    slot: str = "模型",
) -> dict[str, Any]:
    """用一个最小请求试文本或视觉模型能不能调用。

    403 access_denied 是「这个业务空间没被授权用这个模型」，不是 Key 坏了——
    所以光靠「测试连接」（打的是视频接口）测不出来，得单独试。该请求只验证调用权限。
    设置页放这个按钮，就是为了让用户能自己挨个试出可用的模型。
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "回一个字：好"}],
        "max_tokens": 8,
    }

    start = time.perf_counter()
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
    except requests.RequestException as exc:
        return {"ok": False, "model": model, "message": f"网络不通：{exc}"}

    logging_setup.log_http(
        logger, "POST(测试文本模型)", url, resp.status_code,
        elapsed=time.perf_counter() - start,
        error=None if resp.status_code == 200 else resp.text[:250],
        body=f"model={model}",
    )

    if resp.status_code == 200:
        return {"ok": True, "model": model, "message": f"模型「{model}」可以正常调用。"}

    return {
        "ok": False,
        "model": model,
        "message": explain_error(resp.status_code, resp.text, model, slot),
    }


def load_skill_prompt() -> str:
    """读取官方 Skill 正文。

    去掉 YAML frontmatter：那是给 Skill 加载器看的元数据，
    对模型没有意义，留着反而干扰。
    """
    if not SKILL_PATH.exists():
        raise PromptHelperError(
            f"找不到官方 Skill 文件：{SKILL_PATH}。请确认 skills/wan3-pe/SKILL.md 存在。"
        )
    text = SKILL_PATH.read_text(encoding="utf-8")

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]

    return text.strip()


def image_data_uri(path: Path) -> str | None:
    """把本地图片转成 data URI。失败返回 None（降级为只传文字）。"""
    try:
        if path.stat().st_size > MAX_IMAGE_BYTES:
            return None
        mime = mimetypes.guess_type(path.name)[0] or MIME_FALLBACK
        if not mime.startswith("image/"):
            return None
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    except OSError:
        return None


def _asset_manifest(assets: list[dict[str, Any]]) -> str:
    """给模型看的素材清单。

    只给编号、类型和用户自己写的说明。绝不编造素材内容 ——
    对应 Skill 里「引用素材但当前不可得」那一节的处理方式。
    """
    if not assets:
        return "本次没有上传任何素材。"

    lines = ["本次上传的素材（按上传顺序编号）："]
    for asset in assets:
        label = asset.get("label", "?")
        kind = asset.get("kind", "unknown")
        name = asset.get("name", "")
        note = (asset.get("note") or "").strip()
        line = f"- {label}（{kind}，文件名 {name}）"
        if note:
            line += f"：用户说明「{note}」"
        lines.append(line)
    return "\n".join(lines)


def _build_user_text(
    user_prompt: str, assets: list[dict[str, Any]], images: list[tuple[str, str]]
) -> str:
    text = f"{_asset_manifest(assets)}\n\n用户请求：\n{user_prompt.strip()}\n"
    if images:
        labels = "、".join(label for label, _ in images)
        text += (
            f"\n以下图片按编号顺序附在消息里：{labels}。"
            "请按 Skill 要求读取这些图片，为它们建立素材映射。"
        )
    else:
        text += (
            "\n（当前环境读不到素材内容，请按 Skill 中「引用素材但当前不可得」"
            "一节处理：保住引用关系，不补写只有看过素材才能确认的细节。）"
        )
    return text


def _call(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_text: str,
    images: list[tuple[str, str]],
    slot: str = "文本模型",
    operation: str = "提示词整理",
) -> tuple[bool, Any]:
    """发一次整理请求。返回 (是否成功, 文本或错误说明)。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for label, uri in images:
        content.append({"type": "image_url", "image_url": {"url": uri}})
        content.append({"type": "text", "text": f"（以上是{label}）"})

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0.3,
    }

    url = f"{base_url.rstrip('/')}/chat/completions"
    start = time.perf_counter()
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        logging_setup.log_http(
            logger, f"POST({operation})", url, None,
            elapsed=time.perf_counter() - start, error=str(exc),
        )
        return False, ("网络", f"调用失败（网络错误）：{exc}")

    try:
        request_id = resp.json().get("request_id")
    except ValueError:
        request_id = None
    logging_setup.log_http(
        logger, f"POST({operation})", url, resp.status_code,
        request_id=request_id, elapsed=time.perf_counter() - start,
        error=None if resp.status_code == 200 else resp.text[:250],
        body=f"model={model} 附图={len(images)}",
    )

    if resp.status_code != 200:
        # 403/404 是「这个模型用不了」，换个模型还有救，所以单独标出来
        kind = "权限" if resp.status_code in (403, 404) else "其它"
        return False, (kind, explain_error(resp.status_code, resp.text, model, slot))

    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return False, ("其它", f"响应结构异常：{resp.text[:300]}")

    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))

    return True, (text or "").strip()


def call_model(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_text: str,
    images: list[tuple[str, str]],
    *,
    operation: str,
    slot: str = "视觉模型",
) -> str:
    """调用已配置的兼容模型，并沿用统一的错误翻译与请求日志。"""
    ok, result = _call(
        base_url, api_key, model, system_prompt, user_text, images,
        slot=slot, operation=operation,
    )
    if not ok:
        _, message = result
        raise PromptHelperError(message)
    return result


def optimize(
    settings: dict[str, Any],
    *,
    base_url: str,
    api_key: str,
    user_prompt: str,
    assets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """调用 qwen 按官方 Skill 整理提示词。

    有图片就用视觉模型并把图片带上 —— 和官方一致，真的去读素材。

    **不做自动降级。** 视觉模型调不通时直接把错误抛出来，
    因为降级会让工具的能力悄悄低于阿里控制台，而用户要的是能力对等。
    缺权限就去控制台开权限，而不是让工具假装还能用。
    """
    assets = assets or []
    config = settings.get("prompt_helper", {})

    images: list[tuple[str, str]] = []
    if config.get("read_images", True):
        for asset in assets:
            if asset.get("kind") != "image":
                continue
            path = Path(asset.get("path", ""))
            uri = image_data_uri(path) if path.exists() else None
            if uri:
                images.append((asset.get("label", "?"), uri))

    use_vision = bool(images)
    model = (
        (config.get("vision_model") or "").strip() if use_vision
        else (config.get("model") or "qwen-plus").strip()
    )
    if not model:
        raise PromptHelperError(
            "设置里「视觉模型」是空的，但有图片要处理。请填一个能用的视觉模型。"
        )

    ok, result = _call(
        base_url, api_key, model, load_skill_prompt(),
        _build_user_text(user_prompt, assets, images), images,
        slot="视觉模型" if use_vision else "文本模型",
    )

    if not ok:
        _, message = result
        raise PromptHelperError(message)

    return {
        "text": result,
        "model": model,
        "used_vision": use_vision,
        "images_attached": len(images),
        "skill_available": True,
    }
