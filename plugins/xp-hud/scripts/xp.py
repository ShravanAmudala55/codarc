#!/usr/bin/env python3
"""xp-hud: an RPG style XP and level bar for the Claude Code status line.

Claude Code pipes a JSON blob describing the session to the status line
command. This script reads it, keeps a small local save file, and prints a
two line HUD:

    Lv 7 Code Knight  ▰▰▰▰▱▱▱▱▱▱ 312/480 XP  🔥4d
    HP ██████░░░░ 62%  MP ████████░░ 77% (2h14m)  STA ██████░░░░ 59%

HP is context left, MP is the 5 hour session limit left, STA is the weekly
limit left. XP comes from verified outcomes: passing tests, fixed bugs, commits,
builds and lint, and source lines you add (see the caps in CAPS).

Standard library only. Subcommands: statusline, stats, install, uninstall, sync.
"""
import datetime as dt
import json
import os
import shutil
import re
import subprocess
import sys
import time
import traceback

VERSION = "0.1.0"

MAX_LEVEL = 99
XP_PER_USD = 0.0            # spend pays no XP (it is still tracked)
XP_PER_LINE = 0.0           # raw line counts pay no XP; verified edits do (below)
CAPS = {"test": 5, "fix": 3, "commit": 5, "build": 3, "lines": 60}
XP_TEST, XP_FIX, XP_COMMIT, XP_BUILD, XP_CODE_LINE = 15, 25, 20, 8, 0.2
TEST_RE = re.compile(r"\b(pytest|vitest|jest|mocha|cargo test|go test|swift test|bun test|(npm|yarn|pnpm)( run)? test)\b")
BUILD_RE = re.compile(r"\b((npm|yarn|pnpm) run (build|lint)|tsc|eslint|ruff|cargo (build|check|clippy)|go build|swift build)\b")
COMMIT_RE = re.compile(r"\bgit\s+(-\S+\s+)*commit\b")
HASH_RE = re.compile(r"\[[^\]]*?\s([0-9a-f]{7,40})\]")
SRC_RE = re.compile(r"\.(ts|tsx|js|jsx|py|swift|go|rs|java|kt|c|cc|cpp|h|rb|css|html|sh)$")
DAILY_BONUS = 25            # first bit of work each day
STREAK_BONUS_PER_DAY = 5    # added to the daily bonus per streak day
STREAK_BONUS_CAP = 50
ACHIEVEMENT_XP = 100
MAX_DELTA_USD = 25.0        # ignore absurd jumps between two updates
LEVELUP_BANNER_SECONDS = 120
SESSION_KEEP_SECONDS = 14 * 86400
DAYS_KEPT = 60

TITLES = [
    (1, "Rookie Prompter"),
    (5, "Script Squire"),
    (10, "Code Knight"),
    (15, "Refactor Ranger"),
    (20, "Bug Slayer"),
    (30, "Architect"),
    (40, "Archmage"),
    (50, "Grandmaster"),
    (75, "Mythic Engineer"),
    (99, "Legend"),
]

ACHIEVEMENTS = {
    "first_blood": ("First Blood", "Earn your first XP",
                    lambda s, c: s["xp"] > 0),
    "on_a_roll": ("On a Roll", "Reach a 3 day streak",
                  lambda s, c: s["streak"] >= 3),
    "week_warrior": ("Week Warrior", "Reach a 7 day streak",
                     lambda s, c: s["streak"] >= 7),
    "unstoppable": ("Unstoppable", "Reach a 30 day streak",
                    lambda s, c: s["streak"] >= 30),
    "centurion": ("Centurion", "Change 1,000 lines of code",
                  lambda s, c: s["lifetime_lines"] >= 1000),
    "night_owl": ("Night Owl", "Work between midnight and 5am",
                  lambda s, c: c["active"] and c["hour"] < 5),
    "early_bird": ("Early Bird", "Work between 5am and 8am",
                   lambda s, c: c["active"] and 5 <= c["hour"] < 8),
    "marathon": ("Marathon", "Keep a single session going past 3 hours",
                 lambda s, c: c["duration_ms"] >= 3 * 3600 * 1000),
    "near_death": ("Near Death Experience", "Push context past 95 percent",
                   lambda s, c: c["ctx_used"] >= 95),
}


# --------------------------------------------------------------------------
# paths and time
# --------------------------------------------------------------------------

def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")


def home_dir():
    return os.environ.get("XP_HUD_HOME") or os.path.join(
        config_dir(), "xp-hud")


def now():
    """Local time. XP_FAKE_NOW (ISO format) exists so tests can time travel."""
    fake = os.environ.get("XP_FAKE_NOW")
    if fake:
        return dt.datetime.fromisoformat(fake)
    return dt.datetime.now()


# --------------------------------------------------------------------------
# level maths
# --------------------------------------------------------------------------

def threshold(level):
    """Total XP needed to reach `level`."""
    if level <= 1:
        return 0
    return int(round(250 * (level - 1) ** 1.8))


def level_for(xp):
    level = 1
    while level < MAX_LEVEL and xp >= threshold(level + 1):
        level += 1
    return level


def title_for(level):
    name = TITLES[0][1]
    for min_level, title in TITLES:
        if level >= min_level:
            name = title
    return name


# --------------------------------------------------------------------------
# save file
# --------------------------------------------------------------------------

def fresh_state():
    return {
        "version": 1,
        "xp": 0.0,
        "sessions": {},
        "days": {},
        "streak": 0,
        "best_streak": 0,
        "last_active": None,
        "lifetime_usd": 0.0,
        "lifetime_lines": 0,
        "achievements": {},
        "levelup_at": 0,
        "levelup_level": 1,
    }


def load_state():
    path = os.path.join(home_dir(), "state.json")
    state = fresh_state()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state.update(json.load(fh))
    except FileNotFoundError:
        pass
    except (ValueError, OSError):
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
    return state


def save_state(state):
    path = os.path.join(home_dir(), "state.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


class Lock:
    """Best effort cross process lock so parallel sessions do not clobber."""

    def __init__(self):
        self.fh = None

    def __enter__(self):
        os.makedirs(home_dir(), exist_ok=True)
        self.fh = open(os.path.join(home_dir(), ".lock"), "a+")
        try:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        if self.fh:
            self.fh.close()


# --------------------------------------------------------------------------
# earning XP
# --------------------------------------------------------------------------

def dig(data, *keys):
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def touch_day(state, when):
    """Update the streak. Returns the bonus XP for the first work of a day."""
    today = when.date()
    last = state["last_active"]
    if last == today.isoformat():
        return 0
    if last == (today - dt.timedelta(days=1)).isoformat():
        state["streak"] += 1
    else:
        state["streak"] = 1
    state["best_streak"] = max(state["best_streak"], state["streak"])
    state["last_active"] = today.isoformat()
    return DAILY_BONUS + min(STREAK_BONUS_CAP,
                             STREAK_BONUS_PER_DAY * (state["streak"] - 1))


def apply_update(state, data, when):
    """Fold one status line update into the save file. Returns XP gained."""
    ts = when.timestamp()
    sid = data.get("session_id") or "unknown"
    cost = num(dig(data, "cost", "total_cost_usd"))
    lines = int(num(dig(data, "cost", "total_lines_added"))
                + num(dig(data, "cost", "total_lines_removed")))
    duration_ms = num(dig(data, "cost", "total_duration_ms"))

    sess = state["sessions"].get(sid)
    if sess is None:
        # A session we meet part way through is adopted without back pay.
        if duration_ms < 60000:
            sess = {"cost": 0.0, "lines": 0}
        else:
            sess = {"cost": cost, "lines": lines}
        state["sessions"][sid] = sess

    d_cost = cost - sess["cost"]
    if d_cost < 0:          # counter reset
        d_cost = cost
    d_lines = lines - sess["lines"]
    if d_lines < 0:
        d_lines = lines
    d_cost = min(d_cost, MAX_DELTA_USD)
    sess["cost"], sess["lines"], sess["seen"] = cost, lines, ts

    level_before = level_for(state["xp"])
    gained = 0.0
    active = d_cost > 0 or d_lines > 0
    if active:
        gained += d_cost * XP_PER_USD + d_lines * XP_PER_LINE
        state["lifetime_usd"] += d_cost
        state["lifetime_lines"] += d_lines
        state["xp"] += gained
        bonus = touch_day(state, when)
        gained += bonus
        state["xp"] += bonus

    ctx = {
        "hour": when.hour,
        "active": active,
        "duration_ms": duration_ms,
        "ctx_used": num(dig(data, "context_window", "used_percentage")),
    }
    for key, (_, _, check) in ACHIEVEMENTS.items():
        if key not in state["achievements"] and check(state, ctx):
            state["achievements"][key] = when.date().isoformat()
            gained += ACHIEVEMENT_XP
            state["xp"] += ACHIEVEMENT_XP

    if gained:
        today = when.date().isoformat()
        state["days"][today] = state["days"].get(today, 0) + gained

    level_after = level_for(state["xp"])
    if level_after > level_before:
        state["levelup_at"] = ts
        state["levelup_level"] = level_after

    # housekeeping so the save file stays small
    for key in [k for k, v in state["sessions"].items()
                if ts - v.get("seen", 0) > SESSION_KEEP_SECONDS]:
        del state["sessions"][key]
    if len(state["days"]) > DAYS_KEPT:
        for key in sorted(state["days"])[:-DAYS_KEPT]:
            del state["days"][key]
    return gained


def score_call(tool, tinput, response, failed):
    """Turn one finished tool call into verified events (same rules as the desktop mod)."""
    out = []
    tinput = tinput if isinstance(tinput, dict) else {}
    response = response if isinstance(response, dict) else {}
    if tool == "Bash":
        cmd = str(tinput.get("command", ""))
        if "plugin validate" in cmd:
            return out
        stdout = str(response.get("stdout", ""))
        if COMMIT_RE.search(cmd) and not failed and "--amend" not in cmd:
            m = HASH_RE.search(stdout)
            if m:
                out.append(("commit", m.group(1)))
        if TEST_RE.search(cmd):
            out.append(("test", not failed))
        elif BUILD_RE.search(cmd) and not failed:
            out.append(("build", True))
    elif tool in ("Edit", "Write") and not failed:
        path = str(tinput.get("file_path", ""))
        if SRC_RE.search(path):
            n = 0
            for hunk in response.get("structuredPatch") or []:
                for line in hunk.get("lines", []):
                    if line.startswith("+") and not line.startswith("+++"):
                        n += 1
            if n == 0 and tool == "Write":
                n = len(str(tinput.get("content", "")).split("\n"))
            if n > 0:
                out.append(("lines", n))
    return out


def apply_events(state, events, when):
    """Pay verified events with daily caps. Returns XP gained."""
    today = when.date().isoformat()
    d = state.get("xpd")
    if not isinstance(d, dict) or d.get("day") != today:
        failed = bool(d.get("failed")) if isinstance(d, dict) else False
        d = {"day": today, "test": 0, "fix": 0, "commit": 0, "build": 0,
             "lines": 0, "failed": failed}
        state["xpd"] = d
    commits = state.setdefault("commits", [])
    level_before = level_for(state["xp"])
    gain = 0.0
    for kind, val in events:
        if kind == "test":
            if not val:
                d["failed"] = True
            else:
                if d["test"] < CAPS["test"]:
                    d["test"] += 1
                    gain += XP_TEST
                if d["failed"]:
                    d["failed"] = False
                    if d["fix"] < CAPS["fix"]:
                        d["fix"] += 1
                        gain += XP_FIX
        elif kind == "build":
            if d["build"] < CAPS["build"]:
                d["build"] += 1
                gain += XP_BUILD
        elif kind == "commit":
            if val not in commits:
                commits.append(val)
                del commits[:-200]
                if d["commit"] < CAPS["commit"]:
                    d["commit"] += 1
                    gain += XP_COMMIT
        elif kind == "lines":
            before = int(d["lines"])
            d["lines"] = min(CAPS["lines"], d["lines"] + val * XP_CODE_LINE)
            gain += int(d["lines"]) - before
    if gain:
        bonus = touch_day(state, when)
        gain += bonus
        state["xp"] += gain
        state["days"][today] = state["days"].get(today, 0) + gain
        level_after = level_for(state["xp"])
        if level_after > level_before:
            state["levelup_at"] = when.timestamp()
            state["levelup_level"] = level_after
    return gain


# --------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------

def paint(text, code, color):
    return "\033[%sm%s\033[0m" % (code, text) if color else text


def bar(fraction, width=10, fill="█", empty="░"):
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return fill * filled + empty * (width - filled)


def left_color(pct_left):
    if pct_left > 50:
        return "32"
    if pct_left > 20:
        return "33"
    return "31"


def eta(resets_at, ts):
    secs = int(num(resets_at) - ts)
    if secs <= 0:
        return "now"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return "%dd%dh" % (days, hours)
    if hours:
        return "%dh%02dm" % (hours, mins)
    return "%dm" % mins


def xp_progress(xp):
    level = level_for(xp)
    if level >= MAX_LEVEL:
        return level, 1.0, "MAX"
    base, top = threshold(level), threshold(level + 1)
    cur, span = xp - base, top - base
    return level, cur / span, "%d/%d XP" % (cur, span)


def meters(data):
    """(label, pct_left, eta_text) for each meter the data supports."""
    out = []
    used = dig(data, "context_window", "used_percentage")
    out.append(("HP", 100 - num(used), None))
    for label, key in (("MP", "five_hour"), ("STA", "seven_day")):
        used = dig(data, "rate_limits", key, "used_percentage")
        if used is not None:
            out.append((label, 100 - num(used),
                        dig(data, "rate_limits", key, "resets_at")))
    return out


def render(data, state, when, color=True, cols=100):
    ts = when.timestamp()
    level, frac, label = xp_progress(state["xp"])
    streak = state["streak"] if state["last_active"] else 0
    banner = ""
    if ts - state["levelup_at"] < LEVELUP_BANNER_SECONDS and state["levelup_at"]:
        banner = paint("✨ LEVEL UP! Lv %d ✨  " % state["levelup_level"],
                       "1;35", color)

    if cols < 70:
        parts = [banner + paint("Lv%d" % level, "1;36", color),
                 paint(bar(frac, 5, "▰", "▱"), "36", color)]
        for name, left, _ in meters(data):
            parts.append(paint("%s%d" % (name, round(left)),
                               left_color(left), color))
        return " ".join(parts)

    line1 = "%s%s %s  %s %s" % (
        banner,
        paint("Lv %d" % level, "1;36", color),
        paint(title_for(level), "1", color),
        paint(bar(frac, 10, "▰", "▱"), "36", color),
        label)
    if streak:
        line1 += "  🔥%dd" % streak

    segs = []
    for name, left, resets in meters(data):
        seg = "%s %s %d%%" % (name, bar(left / 100.0), round(left))
        if resets is not None and name == "MP":
            seg += " (%s)" % eta(resets, ts)
        segs.append(paint(seg, left_color(left), color))
    return line1 + "\n" + "  ".join(segs)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def read_config():
    try:
        with open(os.path.join(home_dir(), "config.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def cmd_statusline():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    when = now()
    with Lock():
        state = load_state()
        apply_update(state, data, when)
        save_state(state)
    try:
        cols = int(os.environ.get("COLUMNS", "100"))
    except ValueError:
        cols = 100
    color = "NO_COLOR" not in os.environ
    print(render(data, state, when, color=color, cols=cols))

    chain = read_config().get("chain")
    if chain:
        try:
            res = subprocess.run(chain, shell=True, input=raw, text=True,
                                 capture_output=True, timeout=3)
            if res.returncode == 0 and res.stdout.strip():
                print(res.stdout.rstrip("\n"))
        except (subprocess.SubprocessError, OSError):
            pass
    return 0


def cmd_stats():
    state = load_state()
    when = now()
    level, frac, label = xp_progress(state["xp"])
    today = state["days"].get(when.date().isoformat(), 0)
    nxt = "max level" if level >= MAX_LEVEL else "%d XP to Lv %d" % (
        threshold(level + 1) - state["xp"], level + 1)
    print("Lv %d  %s" % (level, title_for(level)))
    print("%s %s   (%s)" % (bar(frac, 20, "▰", "▱"), label, nxt))
    print("")
    print("Total XP:        %d" % state["xp"])
    print("XP today:        %d" % today)
    print("Streak:          %d day(s), best %d" % (
        state["streak"] if state["last_active"] else 0, state["best_streak"]))
    print("Work tracked:    $%.2f estimated API value, %d lines changed" % (
        state["lifetime_usd"], state["lifetime_lines"]))
    print("")
    unlocked = state["achievements"]
    print("Achievements (%d/%d)" % (len(unlocked), len(ACHIEVEMENTS)))
    for key, (name, desc, _) in ACHIEVEMENTS.items():
        if key in unlocked:
            print("  [x] %-22s %s (%s)" % (name, desc, unlocked[key]))
        else:
            print("  [ ] %-22s %s" % (name, desc))
    return 0


def _read_settings(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def _write_settings(path, settings):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _is_ours(status_line):
    command = (status_line or {}).get("command", "")
    return "xp-hud" in command and "statusline" in command


def _installed_script():
    return os.path.join(home_dir(), "xp.py")


def _copy_self():
    dest = _installed_script()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    src = os.path.abspath(__file__)
    if os.path.abspath(dest) != src:
        shutil.copyfile(src, dest)
    return dest


def cmd_install(args):
    force, chain = "--force" in args, "--chain" in args
    settings_path = os.path.join(config_dir(), "settings.json")
    try:
        settings = _read_settings(settings_path)
    except ValueError:
        print("Could not parse %s. Fix the JSON and run setup again." % settings_path)
        return 1

    existing = settings.get("statusLine")
    if existing and not _is_ours(existing) and not (force or chain):
        print("You already have a status line:")
        print("  %s" % existing.get("command", existing))
        print("")
        print("Nothing was changed. Run setup again with one of:")
        print("  --chain   keep your current status line and show it under the XP bar")
        print("  --force   replace it (a backup of settings.json is kept)")
        return 2

    dest = _copy_self()
    if existing and not _is_ours(existing):
        backup = settings_path + ".xp-hud.bak"
        if not os.path.exists(backup):
            shutil.copyfile(settings_path, backup)
        if chain:
            cfg = read_config()
            cfg["chain"] = existing.get("command")
            with open(os.path.join(home_dir(), "config.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=1)

    python = sys.executable.replace("\\", "/") or "python3"
    settings["statusLine"] = {
        "type": "command",
        "command": '"%s" "%s" statusline' % (python, dest.replace("\\", "/")),
    }
    _write_settings(settings_path, settings)
    print("Status line installed. Script: %s" % dest)
    print("Settings updated: %s" % settings_path)
    if chain and existing:
        print("Your previous status line will be shown below the XP bar.")
    print("Send a message in Claude Code and the bar should appear.")
    return 0


def cmd_uninstall():
    settings_path = os.path.join(config_dir(), "settings.json")
    settings = _read_settings(settings_path)
    if not _is_ours(settings.get("statusLine")):
        print("The status line is not set to xp-hud. Nothing to do.")
        return 0
    chain = read_config().get("chain")
    if chain:
        settings["statusLine"] = {"type": "command", "command": chain}
    else:
        del settings["statusLine"]
    _write_settings(settings_path, settings)
    print("Status line removed. Your XP save file is kept in %s" % home_dir())
    return 0


def cmd_sync():
    """SessionStart hook: refresh the installed copy. Must print nothing."""
    try:
        sys.stdin.read()
    except OSError:
        pass
    dest = _installed_script()
    src = os.path.abspath(__file__)
    try:
        if os.path.exists(dest) and os.path.abspath(dest) != src:
            with open(src, "rb") as a, open(dest, "rb") as b:
                if a.read() != b.read():
                    shutil.copyfile(src, dest)
    except OSError:
        pass
    return 0


def cmd_tool(failed):
    """PostToolUse / PostToolUseFailure hook: pay verified work. Must print nothing."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    events = score_call(data.get("tool_name"), data.get("tool_input"),
                        data.get("tool_response"), failed)
    if not events:
        return 0
    with Lock():
        state = load_state()
        apply_events(state, events, now())
        # Lets the desktop band skip events these hooks have already paid.
        state["hook_ts"] = time.time()
        save_state(state)
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "statusline"
    if cmd == "statusline" and len(argv) == 1 and sys.stdin.isatty():
        print(__doc__)
        return 0
    try:
        if cmd == "statusline":
            return cmd_statusline()
        if cmd == "stats":
            return cmd_stats()
        if cmd == "install":
            return cmd_install(argv[2:])
        if cmd == "uninstall":
            return cmd_uninstall()
        if cmd == "sync":
            return cmd_sync()
        if cmd == "tool":
            return cmd_tool(False)
        if cmd == "toolfail":
            return cmd_tool(True)
        if cmd in ("-h", "--help", "help"):
            print(__doc__)
            return 0
        print("Unknown command: %s" % cmd)
        return 1
    except Exception:
        if os.environ.get("XP_HUD_DEBUG"):
            traceback.print_exc()
        if cmd == "statusline":
            print("xp-hud: error (set XP_HUD_DEBUG=1 and run claude --debug)")
            return 0
        if cmd in ("sync", "tool", "toolfail"):
            return 0
        raise


if __name__ == "__main__":
    sys.exit(main(sys.argv))
