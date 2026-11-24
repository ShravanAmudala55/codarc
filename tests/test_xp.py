import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "plugins", "xp-hud", "scripts", "xp.py")

spec = importlib.util.spec_from_file_location("xp", SCRIPT)
xp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(xp)


def payload(sid="s1", cost=0.0, lines=0, duration=1000, ctx=10,
            five=None, week=None):
    data = {
        "session_id": sid,
        "cost": {"total_cost_usd": cost, "total_lines_added": lines,
                 "total_lines_removed": 0, "total_duration_ms": duration},
        "context_window": {"used_percentage": ctx},
    }
    if five is not None or week is not None:
        data["rate_limits"] = {}
        if five is not None:
            data["rate_limits"]["five_hour"] = {
                "used_percentage": five, "resets_at": 1_000_000 + 2 * 3600 + 14 * 60}
        if week is not None:
            data["rate_limits"]["seven_day"] = {
                "used_percentage": week, "resets_at": 1_000_000 + 86400}
    return data


def at(day, hour=14):
    return dt.datetime(2026, 11, day, hour, 0, 0)


class LevelMaths(unittest.TestCase):
    def test_thresholds_increase(self):
        values = [xp.threshold(n) for n in range(1, xp.MAX_LEVEL + 1)]
        self.assertEqual(values[0], 0)
        self.assertEqual(values, sorted(set(values)))

    def test_level_for_boundaries(self):
        self.assertEqual(xp.level_for(0), 1)
        self.assertEqual(xp.level_for(xp.threshold(2) - 1), 1)
        self.assertEqual(xp.level_for(xp.threshold(2)), 2)
        self.assertEqual(xp.level_for(10 ** 9), xp.MAX_LEVEL)

    def test_titles(self):
        self.assertEqual(xp.title_for(1), "Rookie Prompter")
        self.assertEqual(xp.title_for(12), "Code Knight")
        self.assertEqual(xp.title_for(99), "Legend")


class Verified(unittest.TestCase):
    def setUp(self):
        self.state = xp.fresh_state()

    def run_call(self, tool, tin, resp, failed=False):
        return xp.apply_events(self.state, xp.score_call(tool, tin, resp, failed), at(7))

    def test_fail_then_pass_pays_fix_bonus(self):
        self.assertEqual(self.run_call("Bash", {"command": "pytest -q"}, {}, True), 0)
        g = self.run_call("Bash", {"command": "pytest -q"}, {})
        self.assertEqual(g, 15 + 25 + 25)   # tests + fix + daily bonus

    def test_commit_dedupes_and_amend_pays_nothing(self):
        c = {"command": "git commit -m x"}
        r = {"stdout": "[main abc1234] x"}
        self.assertGreater(self.run_call("Bash", c, r), 0)
        self.assertEqual(self.run_call("Bash", c, r), 0)
        self.assertEqual(self.run_call("Bash", {"command": "git commit --amend"}, {"stdout": "[main def5678] x"}), 0)

    def test_daily_cap_on_tests(self):
        for _ in range(8):
            self.run_call("Bash", {"command": "pytest"}, {})
        self.assertEqual(self.state["xpd"]["test"], 5)

    def test_edit_lines_accumulate_and_cap(self):
        resp = {"structuredPatch": [{"lines": ["+a"] * 10}]}
        for _ in range(200):
            self.run_call("Edit", {"file_path": "a.py"}, resp)
        self.assertLessEqual(self.state["xpd"]["lines"], 60)
        self.assertEqual(self.run_call("Edit", {"file_path": "a.md"}, resp), 0)


class Earning(unittest.TestCase):
    def setUp(self):
        self.state = xp.fresh_state()

    def test_cost_and_lines_award_xp(self):
        xp.apply_update(self.state, payload(cost=0.0), at(7))
        self.assertEqual(self.state["xp"], 0)
        gained = xp.apply_update(self.state, payload(cost=1.0, lines=10), at(7))
        # spend and raw lines pay nothing now: 25 daily bonus, 100 first blood
        self.assertAlmostEqual(gained, 25 + 100)
        self.assertAlmostEqual(self.state["lifetime_usd"], 1.0)

    def test_no_double_pay_for_same_numbers(self):
        xp.apply_update(self.state, payload(cost=1.0), at(7))
        before = self.state["xp"]
        gained = xp.apply_update(self.state, payload(cost=1.0), at(7))
        self.assertEqual(gained, 0)
        self.assertEqual(self.state["xp"], before)

    def test_midway_session_is_adopted_without_back_pay(self):
        xp.apply_update(self.state, payload(sid="old", cost=5.0,
                                            duration=3_600_000), at(7))
        self.assertEqual(self.state["xp"], 0)
        xp.apply_update(self.state, payload(sid="old", cost=5.5,
                                            duration=3_700_000), at(7))
        self.assertGreater(self.state["xp"], 49)
        self.assertLess(self.state["xp"], 200)

    def test_counter_reset_pays_new_work(self):
        xp.apply_update(self.state, payload(cost=0.0), at(7))
        xp.apply_update(self.state, payload(cost=2.0), at(7))
        before = self.state["xp"]
        xp.apply_update(self.state, payload(cost=0.5), at(7))
        self.assertAlmostEqual(self.state["xp"] - before, 0)
        self.assertAlmostEqual(self.state["lifetime_usd"], 2.5)

    def test_absurd_jump_is_capped(self):
        xp.apply_update(self.state, payload(cost=0.0), at(7))
        xp.apply_update(self.state, payload(cost=9999), at(7))
        self.assertLess(self.state["lifetime_usd"], 26)

    def test_streak_grows_and_resets(self):
        for day in (5, 6, 7):
            xp.apply_update(self.state, payload(sid="d%d" % day, cost=0), at(day))
            xp.apply_update(self.state, payload(sid="d%d" % day, cost=0.1), at(day))
        self.assertEqual(self.state["streak"], 3)
        self.assertIn("on_a_roll", self.state["achievements"])
        xp.apply_update(self.state, payload(sid="d10", cost=0), at(10))
        xp.apply_update(self.state, payload(sid="d10", cost=0.1), at(10))
        self.assertEqual(self.state["streak"], 1)
        self.assertEqual(self.state["best_streak"], 3)

    def test_idle_session_start_is_not_activity(self):
        xp.apply_update(self.state, payload(cost=0), at(7))
        self.assertEqual(self.state["streak"], 0)
        self.assertIsNone(self.state["last_active"])

    def test_level_up_is_flagged(self):
        xp.apply_update(self.state, payload(cost=0), at(7))
        xp.apply_update(self.state, payload(cost=5.0), at(7))
        self.state["xp"] = 240
        xp.apply_events(self.state, [("test", True)], at(7))
        self.assertGreater(xp.level_for(self.state["xp"]), 1)
        self.assertEqual(self.state["levelup_level"],
                         xp.level_for(self.state["xp"]))

    def test_time_based_achievements(self):
        xp.apply_update(self.state, payload(sid="n", cost=0), at(7, 2))
        xp.apply_update(self.state, payload(sid="n", cost=0.1), at(7, 2))
        self.assertIn("night_owl", self.state["achievements"])
        self.assertNotIn("early_bird", self.state["achievements"])

    def test_near_death_and_marathon(self):
        xp.apply_update(self.state, payload(ctx=96, duration=4 * 3600 * 1000), at(7))
        self.assertIn("near_death", self.state["achievements"])
        self.assertIn("marathon", self.state["achievements"])

    def test_old_sessions_are_pruned(self):
        xp.apply_update(self.state, payload(sid="a", cost=0), at(1))
        xp.apply_update(self.state, payload(sid="b", cost=0), dt.datetime(2026, 12, 20))
        self.assertNotIn("a", self.state["sessions"])


class Rendering(unittest.TestCase):
    def test_two_line_hud(self):
        state = xp.fresh_state()
        state["xp"] = 300
        state["streak"], state["last_active"] = 4, "2026-11-07"
        out = xp.render(payload(ctx=38, five=23, week=41), state,
                        dt.datetime.fromtimestamp(1_000_000), color=False, cols=120)
        line1, line2 = out.split("\n")
        self.assertIn("Lv 2", line1)
        self.assertIn("🔥4d", line1)
        self.assertIn("HP", line2)
        self.assertIn("62%", line2)
        self.assertIn("MP", line2)
        self.assertIn("77%", line2)
        self.assertIn("2h14m", line2)
        self.assertIn("STA", line2)
        self.assertIn("59%", line2)

    def test_api_user_without_rate_limits(self):
        out = xp.render(payload(ctx=10), xp.fresh_state(),
                        dt.datetime.fromtimestamp(1_000_000), color=False, cols=120)
        self.assertIn("HP", out)
        self.assertNotIn("MP", out)

    def test_null_context_percentage(self):
        data = payload()
        data["context_window"]["used_percentage"] = None
        out = xp.render(data, xp.fresh_state(),
                        dt.datetime.fromtimestamp(1_000_000), color=False, cols=120)
        self.assertIn("100%", out)

    def test_narrow_terminal_is_one_line(self):
        out = xp.render(payload(five=50), xp.fresh_state(),
                        dt.datetime.fromtimestamp(1_000_000), color=False, cols=50)
        self.assertNotIn("\n", out)

    def test_levelup_banner_expires(self):
        state = xp.fresh_state()
        when = dt.datetime.fromtimestamp(1_000_000)
        state["levelup_at"], state["levelup_level"] = when.timestamp() - 10, 5
        self.assertIn("LEVEL UP", xp.render({}, state, when, color=False))
        state["levelup_at"] = when.timestamp() - 500
        self.assertNotIn("LEVEL UP", xp.render({}, state, when, color=False))

    def test_eta(self):
        self.assertEqual(xp.eta(1000 + 45 * 60, 1000), "45m")
        self.assertEqual(xp.eta(1000 + 3 * 86400 + 4 * 3600, 1000), "3d4h")
        self.assertEqual(xp.eta(500, 1000), "now")


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ,
                        CLAUDE_CONFIG_DIR=self.tmp.name,
                        XP_HUD_HOME=os.path.join(self.tmp.name, "xp-hud"),
                        XP_FAKE_NOW="2026-11-07T14:00:00",
                        NO_COLOR="1", COLUMNS="120")

    def tearDown(self):
        self.tmp.cleanup()

    def run_xp(self, *args, stdin=""):
        return subprocess.run([sys.executable, SCRIPT, *args], input=stdin,
                              capture_output=True, text=True, env=self.env)

    def test_statusline_persists_state(self):
        self.run_xp("statusline", stdin=json.dumps(payload(cost=0)))
        res = self.run_xp("statusline", stdin=json.dumps(payload(cost=2.0, lines=50)))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Lv ", res.stdout)
        stats = self.run_xp("stats")
        self.assertIn("Total XP:", stats.stdout)
        self.assertIn("[x] First Blood", stats.stdout)

    def test_garbage_input_never_crashes(self):
        for junk in ("", "not json", "[]", "null"):
            res = self.run_xp("statusline", stdin=junk)
            self.assertEqual(res.returncode, 0, (junk, res.stderr))
            self.assertTrue(res.stdout.strip())

    def test_corrupt_save_file_is_recovered(self):
        os.makedirs(self.env["XP_HUD_HOME"])
        with open(os.path.join(self.env["XP_HUD_HOME"], "state.json"), "w") as fh:
            fh.write("{broken")
        res = self.run_xp("statusline", stdin=json.dumps(payload()))
        self.assertEqual(res.returncode, 0)
        self.assertTrue(os.path.exists(
            os.path.join(self.env["XP_HUD_HOME"], "state.json.corrupt")))

    def settings(self):
        with open(os.path.join(self.tmp.name, "settings.json")) as fh:
            return json.load(fh)

    def test_install_into_empty_settings_and_uninstall(self):
        res = self.run_xp("install")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("xp-hud", self.settings()["statusLine"]["command"])
        self.assertTrue(os.path.exists(
            os.path.join(self.env["XP_HUD_HOME"], "xp.py")))
        self.run_xp("uninstall")
        self.assertNotIn("statusLine", self.settings())

    def test_install_keeps_other_settings(self):
        with open(os.path.join(self.tmp.name, "settings.json"), "w") as fh:
            json.dump({"model": "opus", "env": {"A": "1"}}, fh)
        self.run_xp("install")
        s = self.settings()
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["env"], {"A": "1"})

    def test_install_refuses_to_clobber_existing_status_line(self):
        with open(os.path.join(self.tmp.name, "settings.json"), "w") as fh:
            json.dump({"statusLine": {"type": "command", "command": "echo hi"}}, fh)
        res = self.run_xp("install")
        self.assertEqual(res.returncode, 2)
        self.assertEqual(self.settings()["statusLine"]["command"], "echo hi")

    def test_chain_runs_previous_status_line_and_uninstall_restores_it(self):
        with open(os.path.join(self.tmp.name, "settings.json"), "w") as fh:
            json.dump({"statusLine": {"type": "command", "command": "echo PREVIOUS"}}, fh)
        self.assertEqual(self.run_xp("install", "--chain").returncode, 0)
        res = self.run_xp("statusline", stdin=json.dumps(payload()))
        self.assertTrue(res.stdout.rstrip().endswith("PREVIOUS"), res.stdout)
        self.run_xp("uninstall")
        self.assertEqual(self.settings()["statusLine"]["command"], "echo PREVIOUS")

    def test_sync_is_silent_and_refreshes_copy(self):
        self.run_xp("install")
        copy = os.path.join(self.env["XP_HUD_HOME"], "xp.py")
        with open(copy, "w") as fh:
            fh.write("# stale")
        res = self.run_xp("sync", stdin="{}")
        self.assertEqual(res.stdout, "")
        with open(copy) as fh, open(SCRIPT) as src:
            self.assertEqual(fh.read(), src.read())


if __name__ == "__main__":
    unittest.main()
