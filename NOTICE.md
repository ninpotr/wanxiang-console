# Third-party notices

This project links to or drives the following external components. They are
not bundled with this repository unless explicitly stated.

- **video-subtitle-remover** — Apache License 2.0
  https://github.com/YaoFANGUK/video-subtitle-remover
  Used as the optional watermark-removal backend. Its models, ffmpeg binaries
  and desktop UI belong to that project; install it separately.

- **Real-ESRGAN** — BSD 3-Clause License
  https://github.com/xinntao/Real-ESRGAN
  Optional video-upscale backend; `realesrgan-ncnn-vulkan` is downloaded by the
  user and is not committed here.

- **FFmpeg** — LGPL/GPL depending on the build
  https://ffmpeg.org/
  Optional local video tool; users supply their own build.

- **CosyVoice** — Apache License 2.0
  https://github.com/FunAudioLLM/CosyVoice
  The optional voice page expects a user-provided local CosyVoice3 HTTP service.
  Models and the service script are not included.

- **万相3.0 / DashScope** — Alibaba Cloud service
  The app calls the official API; users need their own API key and are bound by
  Alibaba Cloud's terms and pricing.

- **wan3-pe prompt Skill** — official Alibaba Cloud prompt-formatting skill.
  The skill text is not redistributed here. Users can download it from the
  Bailian console and place it under `skills/wan3-pe/SKILL.md`.
