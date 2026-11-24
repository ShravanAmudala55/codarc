---
name: setup
description: Turn on the XP HUD status line by adding it to your Claude Code settings
disable-model-invocation: true
argument-hint: "[--chain | --force]"
---

Set up the XP HUD status line. A plugin cannot switch on a status line by itself, so this one time step writes the `statusLine` entry into the user's `~/.claude/settings.json`.

1. Run this with the Bash tool, passing along any arguments the user gave (`$ARGUMENTS`):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xp.py" install $ARGUMENTS
```

If `python3` is not found, retry with `python`.

2. If the command prints that a status line already exists and exits with code 2, do not retry on your own. Tell the user their current status line, and explain the two options in one line each: `--chain` keeps their existing status line and shows it under the XP bar, `--force` replaces it (a backup of settings.json is kept). Ask which they want.

3. On success, tell the user in one or two sentences that the bar appears after their next message, and that `/xp-hud:stats` shows their full character sheet.

Do not edit any other settings and do not touch the user's save file.
