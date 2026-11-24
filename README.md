<p align="center"><img src="plugins/codarc/logo/logo.svg" alt="codarc logo" width="140"></p>

# codarc

RPG levels for Claude Code. You earn XP for work that actually happened (tests that pass, commits that stick, builds that succeed), not for chatting. This repo is a plugin marketplace with two plugins that share one save file:

| Plugin | What it is |
| --- | --- |
| [codarc](plugins/codarc) | A band above the prompt in the Claude desktop app: level, tier badge, XP bar, streak, fire or water mascot |
| [xp-hud](plugins/xp-hud) | The terminal status line, plus the hooks that pay XP for verified work |

## Install

```bash
claude plugin marketplace add ShravanAmudala55/codarc
claude plugin install xp-hud@shravan-plugins
claude plugin install codarc@shravan-plugins
```

Install `xp-hud` for the terminal bar and the XP hooks, `codarc` for the desktop band, or both. Both read and write `~/.claude/xp-hud/state.json`, so they always show the same level.

## xp-hud: terminal status line

An RPG style level bar for the Claude Code status line. Everything you build with Claude Code earns XP, levels you up and grows a daily streak, while HP, MP and STA meters show how much of your context window, 5 hour session and weekly usage you have left.

```
Lv 6 Script Squire  ▰▰▰▰▰▰▰▰▰▱ 1643/1759 XP  🔥4d
HP ██████░░░░ 62%  MP ████████░░ 77% (2h14m)  STA ██████░░░░ 59%
```

### Install

```bash
claude plugin marketplace add ShravanAmudala55/codarc
claude plugin install xp-hud@shravan-plugins
```

Then, inside Claude Code, run this once:

```
/xp-hud:setup
```

A plugin cannot switch on a status line by itself, so setup writes the `statusLine` entry into `~/.claude/settings.json` for you. The bar appears after your next message.

Already have a status line? Setup will not overwrite it. Run one of these instead:

- `/xp-hud:setup --chain` keeps your current status line and shows it underneath the XP bar
- `/xp-hud:setup --force` replaces it (a backup of your settings is kept)

### The meters

| Meter | Shows what is left of | Source field |
| --- | --- | --- |
| HP | your context window | `context_window.used_percentage` |
| MP | your 5 hour session limit, with a reset countdown | `rate_limits.five_hour` |
| STA | your weekly limit | `rate_limits.seven_day` |

The bars show what is left, so they drain as you work and turn yellow, then red, when you run low. Claude Code only sends the session and weekly numbers for claude.ai Pro and Max subscribers, so API key users just see HP.

### How XP works

| Source | XP | Daily cap |
| --- | --- | --- |
| Tests pass | 15 | 5 |
| Tests pass after failing earlier | +25 | 3 |
| A new `git commit` (amends pay nothing) | 20 | 5 |
| Build or lint passes | 8 | 3 |
| Source lines you add (Edit or Write) | 0.2 per line | 60 XP |
| First work of the day | 25, plus 5 per streak day (up to 50 extra) | once a day |
| Each achievement | 100 | once each |

XP comes from outcomes that can be checked, not from how much you spend or how many lines scroll by. A small hook runs after Bash, Edit and Write calls to spot them. Spend is still tracked, just not paid. Opening a session without doing anything earns nothing.

The XP needed to reach level L is `250 * (L - 1) ^ 1.8`, up to level 99. All the numbers live at the top of `plugins/xp-hud/scripts/xp.py` if you want to make levelling faster or slower.

Titles: Rookie Prompter, Script Squire, Code Knight, Refactor Ranger, Bug Slayer, Architect, Archmage, Grandmaster, Mythic Engineer and Legend.

Achievements: First Blood, On a Roll (3 day streak), Week Warrior (7), Unstoppable (30), Centurion (1,000 lines changed), Night Owl (working between midnight and 5am), Early Bird (5am to 8am), Marathon (a single session past 3 hours) and Near Death Experience (context past 95 percent).

### Commands

- `/xp-hud:stats` shows your character sheet: level, XP, streak, lifetime totals and achievements
- `/xp-hud:setup` turns on the status line

### Privacy

Everything stays on your machine. The plugin makes no network calls and never reads your prompts or transcripts. It looks at the numbers Claude Code hands to a status line (cost totals, lines changed, context percentage, rate limit percentages) and, after each Bash, Edit or Write call, at the command that ran, the file path and whether it succeeded, only to recognise tests, commits, builds and source edits. None of that text is stored: the save file keeps only counters and short commit hashes in `~/.claude/xp-hud/state.json`.

### Uninstall

```bash
python3 ~/.claude/xp-hud/xp.py uninstall
claude plugin uninstall xp-hud@shravan-plugins
```

The first command removes the status line entry (and restores your previous one if you used `--chain`). Your save file is kept, so you can come back to your level later.

### Requirements and notes

- Python 3 on your PATH. The hook and the skills call `python3`; on Windows, where it is often just `python`, edit `plugins/xp-hud/hooks/hooks.json` and the two skills to match.
- Several sessions running at once is fine on macOS and Linux (the save file is locked while it is updated). There is no file locking on Windows yet.
- If the bar ever shows an error, run `claude --debug` with `XP_HUD_DEBUG=1` set to see the traceback.

### Development

```bash
python3 -m unittest discover -s tests -v
claude plugin validate .
claude plugin validate plugins/xp-hud
```

To try changes without installing, start a session with `claude --plugin-dir ./plugins/xp-hud`.

### License

MIT
