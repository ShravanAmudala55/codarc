---
name: stats
description: Show your XP HUD character sheet with level, XP, streak and achievements
disable-model-invocation: true
---

Run this command with the Bash tool and show the output to the user exactly as printed, in a code block, with nothing added before or after it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xp.py" stats
```

If the command fails because `python3` is not found, retry it with `python`.
