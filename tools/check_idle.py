"""检查有没有任务在跑。**重启服务前必须先跑这个。**

用法：
    python tools/check_idle.py && <重启命令>

或者手动：
    python tools/check_idle.py
    echo $?     # 0 = 空闲可以重启，1 = 有任务别动

为什么单独做成脚本：之前我把检查写成 `python -c "print(...)"`，
它只打印了一行提示，退出码还是 0，于是 `&&` 后面的重启照跑不误 ——
检查形同虚设，用户的生成任务被打断（靠恢复机制才救回来）。

退出码是关键，不是提示文字。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"


def main() -> int:
    try:
        with urllib.request.urlopen(f"{BASE}/api/jobs", timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError:
        # 服务没起来，那也没什么可打断的
        print("服务没在跑，可以启动")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"查询失败：{exc}", file=sys.stderr)
        print("查不到状态就是不确定，不要重启。", file=sys.stderr)
        return 1

    jobs = data.get("jobs") or []
    if not jobs:
        print("空闲，可以重启")
        return 0

    print(f"有 {len(jobs)} 个任务在跑，不要重启：", file=sys.stderr)
    for job in jobs:
        print(f"  {job.get('status')} · {job.get('step')} · {job.get('dir')}", file=sys.stderr)
    print("等它跑完再来。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
