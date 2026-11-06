"""成本预估。

官方计费公式：计费时长 = 输入视频时长 + 输出视频时长，按秒计费。
失败不计费。免费额度 30 秒（仅华北2北京，输入+输出合计）。

智能时长（duration = -1）下输出时长由模型决定，提交前无法得知，
所以只能给区间而不是确切数字 —— 这一点必须如实显示给用户。
"""

from __future__ import annotations

from typing import Any

from .config import (
    DURATION_MAX,
    DURATION_MIN,
    DURATION_SMART,
    TOTAL_DURATION_LIMIT_WITH_VIDEO,
)


def unit_price(settings: dict[str, Any], model: str, resolution: str) -> float:
    """单价（元/秒）。查不到按 0 处理，由调用方决定怎么提示。"""
    entry = (
        settings.get("pricing", {})
        .get("models", {})
        .get(model, {})
        .get(resolution)
    )
    if not entry:
        return 0.0
    return round(float(entry.get("list", 0)) * float(entry.get("discount", 1)), 6)


def price_breakdown(
    settings: dict[str, Any], model: str, resolution: str
) -> dict[str, Any]:
    """给界面展示用的价格明细，让用户看到原价和折扣。"""
    entry = (
        settings.get("pricing", {})
        .get("models", {})
        .get(model, {})
        .get(resolution)
    )
    if not entry:
        return {"known": False, "unit_price": 0.0}
    list_price = float(entry.get("list", 0))
    discount = float(entry.get("discount", 1))
    return {
        "known": True,
        "list_price": list_price,
        "discount": discount,
        "unit_price": round(list_price * discount, 6),
        "discounted": discount < 1.0,
    }


def max_output_seconds(input_video_seconds: float) -> int:
    """有视频输入时，输出时长上限 = 30 - 输入时长；无输入时就是 30。"""
    if input_video_seconds <= 0:
        return DURATION_MAX
    return max(0, int(TOTAL_DURATION_LIMIT_WITH_VIDEO - input_video_seconds))


def estimate(
    settings: dict[str, Any],
    model: str,
    resolution: str,
    duration: int,
    input_video_seconds: float = 0.0,
) -> dict[str, Any]:
    """算成本。

    返回结构固定，字段如下：
      exact      True 表示能给出确定金额，False 表示智能时长只能给区间
      estimated  确定金额（元），exact=False 时为 None
      low/high   区间（元），exact=True 时两值相等
    """
    unit = unit_price(settings, model, resolution)
    currency = settings.get("pricing", {}).get("currency", "CNY")
    input_seconds = max(0.0, float(input_video_seconds or 0.0))
    output_cap = max_output_seconds(input_seconds)

    result: dict[str, Any] = {
        "currency": currency,
        "unit_price": unit,
        "price_known": unit > 0,
        "input_video_seconds": round(input_seconds, 2),
        "model": model,
        "resolution": resolution,
        "duration_mode": "smart" if duration == DURATION_SMART else "custom",
        "exact": False,
        "estimated": None,
        "low": None,
        "high": None,
        "note": "",
    }

    if unit <= 0:
        result["note"] = "价格表里没有这个模型/分辨率的单价，无法预估。可在设置页补充。"
        return result

    if duration == DURATION_SMART:
        # 模型自己决定输出多长，界面上只能给范围。
        low_output = DURATION_MIN
        high_output = output_cap
        if high_output < low_output:
            result["note"] = f"输入视频已占 {input_seconds:.1f} 秒，超出 30 秒上限，无法生成。"
            return result
        result["low"] = round((input_seconds + low_output) * unit, 2)
        result["high"] = round((input_seconds + high_output) * unit, 2)
        result["note"] = (
            f"智能时长由模型自动推荐，提交前无法得知实际秒数，"
            f"按 {low_output}~{high_output} 秒估算。以实际生成为准。"
        )
        return result

    output_seconds = int(duration)
    if input_seconds + output_seconds > TOTAL_DURATION_LIMIT_WITH_VIDEO:
        result["note"] = (
            f"输入 {input_seconds:.1f} 秒 + 输出 {output_seconds} 秒 = "
            f"{input_seconds + output_seconds:.1f} 秒，超过 30 秒上限。"
        )
        return result

    billable = input_seconds + output_seconds
    amount = round(billable * unit, 2)
    result.update(
        {
            "exact": True,
            "estimated": amount,
            "low": amount,
            "high": amount,
            "billable_seconds": round(billable, 2),
            "output_seconds": output_seconds,
            "note": f"{input_seconds:.1f}s 输入 + {output_seconds}s 输出 = {billable:g}s 计费",
        }
    )
    return result


def actual_cost(settings: dict[str, Any], model: str, usage: dict[str, Any]) -> dict[str, Any]:
    """任务成功后用返回的 usage 算实际成本。

    usage.duration 就是官方给的计费时长（输入+输出），拿它算最准。
    """
    resolution = _sr_to_resolution(usage.get("SR")) if usage.get("SR") else ""
    unit = unit_price(settings, model, resolution) if resolution else 0.0

    billable = float(usage.get("duration") or 0.0)
    return {
        "currency": settings.get("pricing", {}).get("currency", "CNY"),
        "unit_price": unit,
        "resolution": resolution,
        "input_video_seconds": usage.get("input_video_duration"),
        "output_video_seconds": usage.get("output_video_duration"),
        "billable_seconds": billable,
        "amount": round(billable * unit, 2) if unit > 0 else None,
        "fps": usage.get("fps"),
        "ratio": usage.get("ratio"),
    }


def _sr_to_resolution(sr: Any) -> str:
    """usage.SR 是数字（如 720），映射回档位名。"""
    try:
        value = int(sr)
    except (TypeError, ValueError):
        return ""
    mapping = {480: "480P", 720: "720P", 1080: "1080P"}
    return mapping.get(value, "")
