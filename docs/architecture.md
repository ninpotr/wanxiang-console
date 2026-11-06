# Architecture

The app is a single local FastAPI process plus a static frontend. There is no
build step and no database.

## Request flow

```
browser (web/)
  -> FastAPI routes in app.py
     -> core/jobs.py + core/dashscope.py     generation queue / poll / download
     -> core/assets.py + core/store.py       local asset library
     -> core/video_analysis.py               frame sampling + vision model
     -> core/upscale.py + tools/upscale_video.py
     -> core/watermark.py + tools/watermark/wm_web_runner.py
     -> core/voice*.py                      optional CosyVoice HTTP client
```

Long-running jobs run in background threads; the frontend polls JSON status
endpoints. Uploaded files land in `_staging/`; generated/downloaded media and
metadata land under `outputs/<job-dir>/`.

## Optional integrations

| Feature | Runtime | Where |
|---|---|---|
| Watermark | external VSR Python venv | `tools/watermark/`, `core/watermark.py` |
| Upscale | external Real-ESRGAN + ffmpeg | `tools/upscale_video.py`, `core/upscale.py` |
| Voice | external CosyVoice HTTP service | `core/voice.py`, `core/voice_service.py` |
| Prompt helper | DashScope text/vision model + official Skill | `core/prompt_opt.py` |

Each optional feature fails with a message that points to `SETUP.md`; the rest
of the app keeps working when it is absent.

## Data and persistence

- `settings.json` — local settings, API key, optional integration paths
- `outputs/` — generated videos, metadata, request bodies, reference copies
- `_upscale/jobs/`, `_watermark/jobs/` — task records written as JSON
- `logs/` — process log
- `_staging/` — temporary uploads, cleaned after 7 days

All of these are ignored by git.

## Frontend

`web/index.html` defines views/tabs; `web/app.js` is plain ES modules-free JS
with one section per feature; `web/style.css` holds shared design tokens and
component styles. `core/version.py` computes a stamp so the page can warn when
the served JS is stale.
