# Optional prompt skill

`core/prompt_opt.py` looks for:

```
skills/wan3-pe/SKILL.md
```

The official `wan3-pe` prompt-formatting Skill is not redistributed in this
repository. Download it from the Alibaba Cloud Bailian console
(《万相3.0-视频生成 → 提示词技巧》) and place it at the path above, then enable
「提示词整理」 in the settings page.

Without the file, the rest of the app works normally; the prompt-optimize
endpoint returns a clear error if called.
