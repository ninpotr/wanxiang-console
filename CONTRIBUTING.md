# Contributing

## 本地开发

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python app.py
```

浏览器打开 <http://127.0.0.1:8000>。前端是 `web/` 下的原生 HTML/CSS/JS，
没有构建步骤；改完刷新页面即可。

## 测试

```bash
pytest -q tests
```

新增功能请补测试，尤其是：

- `tests/test_watermark.py`：API 参数校验、任务状态、落盘、旧任务兼容
- `tests/test_voice.py`：无外部服务时应跳过或 mock，不要依赖个人模型
- `tests/test_video_analysis.py`：外部模型调用要 mock

## 代码约定

- 服务端只监听 `127.0.0.1`，不要改成对外监听
- `settings.json`、`outputs/`、`logs/`、`_staging/`、`_watermark/` 等运行数据
  已在 `.gitignore` 中；提交前确认没有 API Key 和个人素材
- 可选依赖（VSR / CosyVoice / Real-ESRGAN）缺失时，错误信息要告诉用户
  去 `SETUP.md` 的哪一节，而不是静默降级
- 前端改动涉及缓存时，使用 `?v=` 或现有的版本戳机制

## 提交 PR

1. 从 `main` 开分支；
2. 写清改动目的、影响范围、测试方式；
3. 不要提交模型、二进制、个人视频/音频、API Key 或日志。
