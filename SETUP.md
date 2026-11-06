# 安装与可选依赖

主控制台只需要 Python 和 `requirements.txt` 就能跑。下面这些是可选能力，
按需安装，不装不影响其他页面。

## 0. 主控制台

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy settings.example.json settings.json      # Windows
# cp settings.example.json settings.json      # macOS/Linux
python app.py
```

Windows 也可以直接双击 `start.bat`。打开 <http://127.0.0.1:8000> 后进「设置」
填 API Key、选地域、点「测试连接」。

> API Key 只写入本机 `settings.json`，该文件已被 `.gitignore` 忽略，不会上传。

## 1. 去水印（可选，但需要额外环境）

网页的「后处理 → AI 去水印」调用
[video-subtitle-remover](https://github.com/YaoFANGUK/video-subtitle-remover) 的原生推理。
本仓库已经把网页 runner 放在 `tools/watermark/`，**不需要修改 VSR 仓库**。

### 1.1 克隆 VSR

推荐放在控制台仓库的同级目录：

```bash
git clone https://github.com/YaoFANGUK/video-subtitle-remover.git ../video-subtitle-remover
```

如果放在别处，启动控制台前设置环境变量：

```bat
set WAN3_VSR_ROOT=D:\path\to\video-subtitle-remover
```

### 1.2 给 VSR 建独立 Python 环境

VSR 官方要求 Python 3.12+。在 VSR 目录执行：

```bat
cd ..\video-subtitle-remover
python -m venv .venv
.venv\Scripts\activate
```

先装 PyTorch（CPU 或 CUDA 二选一）：

```bat
:: CPU（任何机器都能跑，速度慢）
pip install torch==2.7.0 torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cpu

:: NVIDIA GPU（50 系 / Blackwell 用 cu128；40 系也可用 cu128）
pip install torch==2.7.0 torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cu128
```

再装 Paddle（OCR 用 CPU 版即可）、PySide6（VSR 的配置模块依赖它）和 VSR 依赖：

```bat
pip install paddlepaddle==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
pip install PySide6==6.8.3
pip install -r requirements.txt
```

VSR 的模型和 ffmpeg 已经包含在它的仓库里，第一次处理时会自动合并。

最后回到控制台目录，应用一个可选但推荐的兼容补丁（修模型重复合并、
Paddle 在某些 Windows 环境下的加载顺序）：

```bat
cd ..\wanxiang-console
.venv\Scripts\python.exe tools\watermark\patch_vsr.py ..\video-subtitle-remover
```

启动控制台后，去「后处理 → AI 去水印」选资产或上传文件，先点「检测并框选」，
确认红框后再处理。

> 没有 NVIDIA GPU 时也能用，只是 STTN / LAMA 会慢很多；ProPainter 很吃显存，
> 12GB 显卡建议把「ProPainter 最大加载帧数」调到 20–30。

## 2. 视频超清（可选）

`tools/upscale_video.py` 需要：

1. **Real-ESRGAN ncnn Vulkan**
   去 <https://github.com/xinntao/Real-ESRGAN/releases> 下载 Windows 的
   `realesrgan-ncnn-vulkan-*.zip`，解压到：

   ```
   tools/bin/realesrgan/realesrgan-ncnn-vulkan.exe
   ```

2. **ffmpeg + ffprobe**
   系统 PATH 里有也可以；想用项目自带逻辑，则放到：

   ```
   tools/bin/ffmpeg/<任意子目录>/bin/ffmpeg.exe
   tools/bin/ffmpeg/<任意子目录>/bin/ffprobe.exe
   ```

   推荐 gyan.dev 的 full build（含 libx264）。

## 3. 声音工作台（可选）

声音页只是客户端，必须自备一个兼容的 **CosyVoice3 HTTP 服务**（模型、venv
和 `tts_server.py` 都不在本仓库里）。在 `settings.json` 的 `voice` 段填入：

```json
{
  "voice": {
    "enabled": true,
    "service_python": "D:\\path\\to\\venv\\Scripts\\python.exe",
    "service_script": "D:\\path\\to\\tts_server.py",
    "service_url": "http://127.0.0.1:9880",
    "model_dir": "D:\\path\\to\\Fun-CosyVoice3-0.5B",
    "auto_start": true,
    "idle_unload_seconds": 600
  }
}
```

不配置时请保持 `"enabled": false`，其他页面完全不受影响。

## 4. 提示词整理（可选）

本仓库不包含阿里云官方的 `wan3-pe` Skill 正文。需要时从百炼控制台的
《万相3.0-视频生成 → 提示词技巧》下载，放到：

```
skills/wan3-pe/SKILL.md
```

然后在设置页打开「提示词整理」。没有这个文件时，整理接口会明确报错，
不会影响直接生成。

## 5. 常见问题

- **`ModuleNotFoundError: fastapi`**：确认激活了 `.venv` 并执行了 `pip install -r requirements.txt`
- **去水印提示找不到环境**：检查 `WAN3_VSR_ROOT` 和 `video-subtitle-remover/.venv/Scripts/python.exe`
- **去水印自动检测找不到**：纯 logo 用「LAMA 直接框选」；水印在中间或移动时手动框
- **声音页连不上**：先确认本地 CosyVoice 服务的 `service_url` 可访问
- **超清提示找不到 realesrgan**：按第 2 节放到 `tools/bin/realesrgan/`
