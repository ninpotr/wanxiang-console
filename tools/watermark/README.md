# AI 去水印模块

这里是控制台「后处理 → AI 去水印」背后的执行器，直接调用
[video-subtitle-remover](https://github.com/YaoFANGUK/video-subtitle-remover)
原生 `SubtitleRemover`，所以 STTN / LAMA / ProPainter / OpenCV、检测模型、
STTN/ProPainter 参数、A/B 区间与桌面版一致。

## 文件

- `wm_web_runner.py`：网页控制台使用，接收 JSON 任务参数，输出
  `@@WM_PROGRESS {...}` 进度行和实时对比图
- `wm_remove.py`：命令行/批量入口，也用于只检测预览
- `patch_vsr.py`：给本地 VSR 打两个兼容补丁（可选但推荐）

## 安装 VSR

完整步骤见仓库根目录的 [SETUP.md](../../SETUP.md)。核心是：

1. 把 `video-subtitle-remover` 克隆到控制台同级目录；
2. 在 VSR 目录创建 `.venv`，安装 PyTorch、Paddle 和 VSR requirements；
3. 运行 `python tools/watermark/patch_vsr.py ..\video-subtitle-remover`；
4. 如果 VSR 不在同级目录，设置 `WAN3_VSR_ROOT`。

## 命令行用法

用 VSR 的 Python 运行（它需要 `backend` 和模型）：

```bat
..\video-subtitle-remover\.venv\Scripts\python.exe tools\watermark\wm_remove.py ^
  -i "input.mp4" --preset auto --engine lama
```

支持：

- `--preset auto|doubao|wanxiang|liblib|libtv|jimeng|kling|hailuo|generic-br|...`
- `--box x1 y1 x2 y2`（相对坐标）或 `--pixel-box`
- `--detect-only --preview-out preview.jpg`
- `--engine sttn|lama`
- 批量传文件夹

纯 logo 水印用控制台的「LAMA 直接框选」；自动检测只找文字水印。
