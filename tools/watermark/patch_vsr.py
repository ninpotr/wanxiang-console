# -*- coding: utf-8 -*-
"""给本地 video-subtitle-remover 打两个小兼容补丁（可重复执行）。

用法：
    python tools/watermark/patch_vsr.py [VSR目录]

不传目录时读取 WAN3_VSR_ROOT，默认找控制台仓库同级的 video-subtitle-remover。

补丁内容：
1. model_config.py 的 bit-lama.pt -> big-lama.pt，避免每次启动都重复合并
   197MB 的 LAMA 模型；
2. SubtitleDetect 加载 Paddle 前屏蔽 langchain_text_splitters，并跳过
   PaddleX 的联网模型源检查，避免某些 Windows 环境下的 torch DLL 加载顺序问题。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_root(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("WAN3_VSR_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # __file__ 在 tools/watermark/ 下，parents[3] 才是仓库的上一级
    return (Path(__file__).resolve().parents[3] / "video-subtitle-remover").resolve()


def patch_model_config(root: Path) -> bool:
    path = root / "backend" / "tools" / "model_config.py"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    new = text.replace(
        "merge_big_file_if_not_exists(self.LAMA_MODEL_DIR, 'bit-lama.pt')",
        "merge_big_file_if_not_exists(self.LAMA_MODEL_DIR, 'big-lama.pt')",
    )
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def patch_subtitle_detect(root: Path) -> bool:
    path = root / "backend" / "tools" / "subtitle_detect.py"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK" in text:
        return False
    old = "    def text_detector(self):\n        import paddle\n"
    new = (
        "    def text_detector(self):\n"
        "        import os\n"
        "        os.environ.setdefault(\"PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK\", \"True\")\n"
        "        # 某些 Windows 环境里，PaddleX 的 langchain 兼容层会拉起另一份 torch；\n"
        "        # 这里让它走 MockSplitter 分支。\n"
        "        sys.modules.setdefault(\"langchain_text_splitters\", None)\n"
        "        import paddle\n"
    )
    if old not in text:
        return False
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True


def main() -> int:
    root = resolve_root(sys.argv[1] if len(sys.argv) > 1 else None)
    if not (root / "backend").exists():
        print(f"找不到 VSR 目录：{root}", file=sys.stderr)
        return 1
    changed = []
    if patch_model_config(root):
        changed.append("model_config.py")
    if patch_subtitle_detect(root):
        changed.append("subtitle_detect.py")
    print(f"VSR：{root}")
    print("已修改：" + (", ".join(changed) if changed else "无（补丁已存在或版本不同）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
