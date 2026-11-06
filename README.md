# 万相3.0 视频创作台（本地控制台）

一个跑在本机的万相3.0 视频生成工作台：仿照阿里云百炼控制台的交互，直连
DashScope 官方 API。API Key 只保存在本机 `settings.json`，服务只监听
`127.0.0.1`，不对外暴露。

## 功能

- **创作**：文生视频、参考图/视频/音频、首尾帧、时长/分辨率/比例、声音开关
- **智能改写 / 智能时长**：使用官方 `prompt_extend` 和 `duration: -1`
- **资产库**：本地保存视频、参数、提示词和参考素材副本；支持收藏、标签、复用参数
- **视频分析大师**：抽帧 + 视觉模型分析剪辑节奏、黑边、响度等线索
- **后处理**
  - 视频超清：Real-ESRGAN 超采样，分辨率不变或升到 1080P/2K/4K
  - AI 去水印：完整网页工作台，支持多选区、A/B、STTN / LAMA / ProPainter /
    OpenCV、检测模型、高级参数、任务队列和实时对比
  - 图片拼接：多格画布、导出 PNG
- **声音（可选）**：接本机 CosyVoice3 HTTP 服务做零样本克隆和台词合成；
  模型和本地服务不随本仓库分发
- **拖拽素材**：图片、视频、音频可直接拖进页面，按当前标签页自动归类

## 快速开始

要求：Python 3.10+（需要去水印时建议 3.12+，因为 Paddle / VSR 对版本更敏感）。

```bash
git clone <your-repo-url> wanxiang-console
cd wanxiang-console
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate
pip install -r requirements.txt
copy settings.example.json settings.json   # Windows；Linux 用 cp
python app.py
```

打开 <http://127.0.0.1:8000>，在「设置」里填 DashScope API Key，保存后点
「测试连接」。也可以直接双击 `start.bat`（Windows）。

各可选能力的安装步骤见 **[SETUP.md](SETUP.md)**：

- 去水印：需要额外克隆 `video-subtitle-remover`，并创建它自己的 Python 环境
- 视频超清：需要 Real-ESRGAN ncnn Vulkan 和 ffmpeg
- 声音：需要自备 CosyVoice3 本地服务
- 提示词整理：需要官方 `wan3-pe` Skill（仓库不内置）

没有安装可选依赖时，其余功能不受影响。

架构说明见 [docs/architecture.md](docs/architecture.md)。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q tests
```

## 目录结构

```
app.py                 FastAPI 路由与启动入口
core/
  config.py            配置、地域、模型资料
  dashscope.py         创建任务 / 轮询 / 下载
  assets.py            资产库与云端补下载
  jobs.py              生成任务编排
  upscale.py           超清任务管理
  watermark.py         去水印任务管理（调用 tools/watermark runner）
  video_analysis.py    抽帧、响度、黑边、视觉模型分析
  voice*.py            可选的本机 CosyVoice 客户端与进程管理
web/                   前端页面（原生 JS）
tools/
  upscale_video.py     超清执行器
  watermark/           去水印 CLI 与网页 runner
tests/                 单元测试
```

## 数据与隐私

- `settings.json` 保存 API Key，已在 `.gitignore` 中；不要提交
- `outputs/`、`_staging/`、`logs/`、`_watermark/`、`_upscale/` 都是运行数据，
  默认不进入版本库
- 服务仅监听 `127.0.0.1`；没有遥测、没有远程上报
- 生成/分析会产生 DashScope API 费用，具体价格以阿里云百炼为准

## 第三方组件

去水印功能使用 [video-subtitle-remover](https://github.com/YaoFANGUK/video-subtitle-remover)
（Apache-2.0），需要单独安装，不随本仓库分发。视频超清使用
[Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN)（外部工具）。详见
[NOTICE.md](NOTICE.md)。

## 免责声明

本项目是独立的本地工具，与阿里云无隶属或背书关系。「万相」「百炼」等名称
归其权利人所有，仅用于说明所对接的 API。模型价格、参数和可用性以阿里云官方
文档为准。

## License

MIT，见 [LICENSE](LICENSE)。欢迎提交 issue / PR；开发约定见
[CONTRIBUTING.md](CONTRIBUTING.md)。
