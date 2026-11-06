"""配置读写。

所有可变项都落在项目根目录的 settings.json，界面上的「设置」页直接改这个文件。
价格表刻意做成可编辑的：官方目前是限时折扣价，改价时不用改代码。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "settings.json"

# 地域 -> 域名。shared 为共享域名（无需业务空间 ID），workspace 为业务空间专属域名。
# 空字符串表示该地域官方未提供共享域名，只能走业务空间专属域名。
REGIONS: dict[str, dict[str, str]] = {
    "beijing": {
        "label": "华北2（北京）",
        "shared": "https://dashscope.aliyuncs.com/api/v1",
        "workspace": "https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1",
        "compatible": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    "singapore": {
        "label": "新加坡",
        "shared": "https://dashscope-intl.aliyuncs.com/api/v1",
        "workspace": "https://{workspace_id}.ap-southeast-1.maas.aliyuncs.com/api/v1",
        "compatible": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    },
    "hongkong": {
        "label": "中国香港",
        "shared": "https://cn-hongkong.dashscope.aliyuncs.com/api/v1",
        "workspace": "https://{workspace_id}.cn-hongkong.maas.aliyuncs.com/api/v1",
        "compatible": "https://cn-hongkong.dashscope.aliyuncs.com/compatible-mode/v1",
    },
    "tokyo": {
        "label": "日本（东京）",
        "shared": "",
        "workspace": "https://{workspace_id}.ap-northeast-1.maas.aliyuncs.com/api/v1",
        "compatible": "https://{workspace_id}.ap-northeast-1.maas.aliyuncs.com/compatible-mode/v1",
    },
    "frankfurt": {
        "label": "德国（法兰克福）",
        "shared": "",
        "workspace": "https://{workspace_id}.eu-central-1.maas.aliyuncs.com/api/v1",
        "compatible": "https://{workspace_id}.eu-central-1.maas.aliyuncs.com/compatible-mode/v1",
    },
    "virginia": {
        "label": "美国（弗吉尼亚）",
        "shared": "",
        "workspace": "https://{workspace_id}.us-east-1.maas.aliyuncs.com/api/v1",
        "compatible": "https://{workspace_id}.us-east-1.maas.aliyuncs.com/compatible-mode/v1",
    },
}

MODELS = ["wan3.0-video", "wan3.0-video-prime"]

# 模型资料。来自官方文档（万相3.0-视频生成使用指南 + API参考 + 计费文档）。
# 放在这里是为了界面上能像控制台那样点开看，不用去翻文档。
MODEL_INFO: dict[str, dict[str, Any]] = {
    "wan3.0-video": {
        "display": "万相3.0-视频生成",
        "tagline": "all-in-one · 四模态全能参考",
        "description": (
            "万相3.0 是 all-in-one 视频生成模型，统一支持参考、编辑、复刻、驱动等多种"
            "创作功能。四模态全能参考（文本 / 图像 / 视频 / 音频），最长支持 30 秒视频"
            "生成，可解析文档与网页。输出 30fps、自带音频的视频。"
        ),
        "input_modalities": ["文本", "图像", "视频", "音频", "文档", "网页"],
        "output_modalities": ["视频（含音频）"],
        "rpm": 300,
        "speed": "标准",
    },
    "wan3.0-video-prime": {
        "display": "万相3.0-视频生成（高速版）",
        "tagline": "能力对齐标准版 · 端到端更快",
        "description": (
            "高速版，能力对齐标准版，端到端速度显著提升，单价更高。"
            "素材限制、参数取值范围与标准版完全一致。"
        ),
        "input_modalities": ["文本", "图像", "视频", "音频", "文档", "网页"],
        "output_modalities": ["视频（含音频）"],
        "rpm": 300,
        "speed": "高速",
    },
}

# 创作模式。控制台用这几个标签筛选模型；在这里它们既是说明也是入口，
# 点一下能告诉你这个模式需要准备什么素材。
MODES: list[dict[str, str]] = [
    {
        "key": "all",
        "name": "全能参考",
        "desc": "图片、视频、音频任意组合，模型自动理解你的意图。",
        "setup": "素材一起传，提示词里用「图1」「视频1」「音频1」分别指代。",
    },
    {
        "key": "frames",
        "name": "首尾帧",
        "desc": "严格指定视频的第一帧和最后一帧，画面完全可控。",
        "setup": "点左上角那两个格子放图。只放首帧就是首帧生视频；两个都放就是首尾帧。",
    },
    {
        "key": "i2v",
        "name": "图生视频",
        "desc": "用一张图作为起点，后续画面由模型发挥。",
        "setup": "往「首帧」格子里放一张图即可。",
    },
    {
        "key": "t2v",
        "name": "文生视频",
        "desc": "只给提示词，不用任何素材。",
        "setup": "不传素材，直接写提示词。",
    },
    {
        "key": "r2v",
        "name": "参考生视频",
        "desc": "参考素材里的形象、动作、音色，但不锁定首尾画面。",
        "setup": "用「参考媒体」上传图片 / 视频 / 音频。",
    },
    {
        "key": "edit",
        "name": "视频编辑 / 延长",
        "desc": "传入视频 + 编辑指令：改风格、换元素、向前后延长。",
        "setup": "用「参考媒体」传一段视频，提示词写「把…换成…」或「向后续写…」。",
    },
]

RESOLUTIONS = ["1080P", "720P", "480P"]

RATIOS = ["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]

RATIO_LABELS = {
    "adaptive": "自动",
    "21:9": "21:9",
    "16:9": "16:9",
    "4:3": "4:3",
    "1:1": "1:1",
    "3:4": "3:4",
    "9:16": "9:16",
}

# 官方文档：无视频输入时 2~30 秒；-1 为智能时长；有视频输入时 输入+输出 <= 30 秒。
DURATION_SMART = -1
DURATION_MIN = 2
DURATION_MAX = 30
TOTAL_DURATION_LIMIT_WITH_VIDEO = 30

DEFAULT_SETTINGS: dict[str, Any] = {
    "api_key": "",
    "region": "beijing",
    # shared = 共享域名（简单）；workspace = 业务空间专属域名（更高吞吐）
    "endpoint_style": "shared",
    "workspace_id": "",
    "model": "wan3.0-video",
    "output_dir": "outputs",
    "poll_interval_seconds": 15,
    "download_retries": 3,
    "keep_reference_copies": True,
    # 提示词整理层：官方 wan3-pe Skill 的复刻，默认开启。
    #
    # 这是两层中的第一层，两层同时生效：
    #   本地整理（本项）—— 把松散输入整理成结构化请求，提交前可预览可编辑
    #   服务端智能改写  —— prompt_extend=true（默认），提交后由阿里自动做
    #
    # 本地整理与服务端智能改写可以同时开启；本地结果仅作参考。
    # 注意：本地这一层的产物**和服务端改写后的结果不一定一致**，
    # 所以界面上标注为「仅供参考」。
    "prompt_helper": {
        # 需要官方 wan3-pe Skill 才能用；文件不在时接口会明确报错。
        # 建议先在设置页测试模型权限，再决定是否开启。
        "enabled": False,
        "model": "qwen-plus",
        "vision_model": "qwen-vl-max-latest",
        "read_images": True,
    },
    # 可选的本地 CosyVoice3 语音服务。模型和 venv 不随本仓库分发；
    # 按 SETUP.md 配好后，在设置页填入 service_python / service_script /
    # model_dir，或直接改 settings.json。默认关闭，不影响其他功能。
    "voice": {
        "enabled": False,
        "service_python": "",
        "service_script": "",
        "service_url": "http://127.0.0.1:9880",
        "model_dir": "",
        "auto_start": False,
        "idle_unload_seconds": 600,
    },
    # 单位：元/秒。list = 官方原价，discount = 折扣系数，实付 = list * discount。
    "pricing": {
        "currency": "CNY",
        "models": {
            "wan3.0-video": {
                "480P": {"list": 0.3, "discount": 0.7},
                "720P": {"list": 0.6, "discount": 0.7},
                "1080P": {"list": 1.2, "discount": 0.7},
            },
            "wan3.0-video-prime": {
                "480P": {"list": 0.45, "discount": 1.0},
                "720P": {"list": 0.9, "discount": 1.0},
                "1080P": {"list": 1.8, "discount": 1.0},
            },
        },
        "free_quota_seconds": 30,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """用 override 覆盖 base，嵌套 dict 递归合并。

    这样将来新增配置项时，老 settings.json 不会缺字段。
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings() -> dict[str, Any]:
    """读取配置；文件不存在或缺字段时用默认值补齐。"""
    if not SETTINGS_PATH.exists():
        save_settings(DEFAULT_SETTINGS)
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    return _deep_merge(DEFAULT_SETTINGS, raw)


def save_settings(settings: dict[str, Any]) -> None:
    """写配置。只写已知字段，避免界面传入的临时字段被持久化。"""
    clean = _deep_merge(DEFAULT_SETTINGS, settings)
    SETTINGS_PATH.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_base_url(settings: dict[str, Any]) -> str:
    """按当前配置拼出 API base url。

    共享域名不需要业务空间 ID；专属域名必须填，缺了就抛错而不是悄悄降级，
    否则用户会拿着 401 查半天。
    """
    region = settings.get("region", "beijing")
    spec = REGIONS.get(region)
    if spec is None:
        raise ValueError(f"未知地域: {region}")

    style = settings.get("endpoint_style", "shared")
    if style == "workspace":
        workspace_id = (settings.get("workspace_id") or "").strip()
        if not workspace_id:
            raise ValueError("选择「业务空间专属域名」时必须填写业务空间 ID")
        return spec["workspace"].format(workspace_id=workspace_id)

    shared = spec["shared"]
    if not shared:
        raise ValueError(
            f"{spec['label']} 没有共享域名，请在设置里改用「业务空间专属域名」并填写业务空间 ID"
        )
    return shared


def get_compatible_base_url(settings: dict[str, Any]) -> str:
    """OpenAI 兼容接口的 base url，用于提示词整理那一层调用 qwen。"""
    region = settings.get("region", "beijing")
    spec = REGIONS.get(region)
    if spec is None:
        raise ValueError(f"未知地域: {region}")

    template = spec.get("compatible", "")
    if not template:
        raise ValueError(f"{spec['label']} 没有可用的兼容接口地址")

    if "{workspace_id}" in template:
        workspace_id = (settings.get("workspace_id") or "").strip()
        if not workspace_id:
            raise ValueError("该地域的兼容接口需要业务空间 ID，请在设置里填写")
        return template.format(workspace_id=workspace_id)
    return template


def mask_key(api_key: str) -> str:
    """给界面看的脱敏 Key，不用于任何请求。"""
    if not api_key:
        return ""
    if len(api_key) <= 12:
        return api_key[:4] + "****"
    return f"{api_key[:8]}****{api_key[-4:]}"


def public_settings() -> dict[str, Any]:
    """返回给前端的配置：带上脱敏 Key 和地域/模型等选项表，但不回传真实 Key。"""
    settings = load_settings()
    return {
        "settings": {
            **settings,
            "api_key": "",
            "has_api_key": bool(settings.get("api_key")),
            "api_key_masked": mask_key(settings.get("api_key", "")),
        },
        "options": {
            "regions": [
                {
                    "value": key,
                    "label": spec["label"],
                    "has_shared": bool(spec["shared"]),
                }
                for key, spec in REGIONS.items()
            ],
            "models": MODELS,
            "resolutions": RESOLUTIONS,
            "ratios": [{"value": r, "label": RATIO_LABELS[r]} for r in RATIOS],
            "duration": {
                "smart": DURATION_SMART,
                "min": DURATION_MIN,
                "max": DURATION_MAX,
                "total_limit_with_video": TOTAL_DURATION_LIMIT_WITH_VIDEO,
            },
        },
    }
