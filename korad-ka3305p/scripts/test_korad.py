#!/usr/bin/env python3
"""Tests for korad.py. No device needed: every daemon here runs on FakeSerial
in an isolated KORAD_HOME. Run from this directory:

    python3 -m unittest test_korad -v
"""
import contextlib
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import korad as k  # noqa: E402

UTC = dt.timezone.utc


def guard(limits, hours=1.0, mode=None, note="test"):
    return {"limits": limits, "mode": mode, "note": note,
            "expires_utc": (dt.datetime.now(UTC) + dt.timedelta(hours=hours)).isoformat()}


def sp(v1=0, i1=10, v2=0, i2=10):
    return {"v1": v1, "i1": i1, "v2": v2, "i2": i2}


# ---------------------------------------------------------------- units

class ParseValue(unittest.TestCase):
    def test_accepted_voltage(self):
        for text, want in [("3.3", 330), ("3.3V", 330), ("3.3v", 330), ("3300mV", 330),
                           (".5", 50), ("0", 0), ("31", 3100), ("31.00", 3100), ("12.34 V", 1234)]:
            with self.subTest(text=text):
                self.assertEqual(k.parse_value(text, "v"), want)

    def test_accepted_current(self):
        for text, want in [("0.05A", 50), ("50mA", 50), ("5.1", 5100), (".001", 1), ("1mA", 1)]:
            with self.subTest(text=text):
                self.assertEqual(k.parse_value(text, "i"), want)

    def test_trailing_dot_is_accepted(self):
        # Recorded behavior: "3." reads as 3.00 V.
        self.assertEqual(k.parse_value("3.", "v"), 300)

    def test_refused(self):
        for text, kind in [("3v3", "v"), ("3.333", "v"), ("0.0005A", "i"), ("31.01", "v"),
                           ("5.101A", "i"), ("-1", "v"), ("", "v"), ("   ", "i"), ("abc", "v"),
                           ("3.3.3", "v"), ("1e1", "v"), ("3333.3mV", "v")]:
            with self.subTest(text=text):
                with self.assertRaises(k.KoradError) as cm:
                    k.parse_value(text, kind)
                self.assertEqual(cm.exception.code, "E_USAGE")

    def test_unit_of_wrong_kind(self):
        for text, kind in [("3.3A", "v"), ("50mV", "i"), ("3.3mA", "v"), ("1V", "i")]:
            with self.subTest(text=text):
                with self.assertRaises(k.KoradError) as cm:
                    k.parse_value(text, kind)
                self.assertIn("not valid", cm.exception.message)

    def test_precision_message_says_refused_not_rounded(self):
        with self.assertRaises(k.KoradError) as cm:
            k.parse_value("3.333", "v")
        self.assertIn("not rounded", cm.exception.message)


class ParseDuration(unittest.TestCase):
    def test_forms(self):
        for text, want in [("90s", 90), ("30m", 1800), ("4h", 14400), ("1h30m", 5400),
                           ("1h30m15s", 5415), ("0.5s", 0.5), ("4", 4.0)]:
            with self.subTest(text=text):
                self.assertEqual(k.parse_duration(text), want)

    def test_refused(self):
        # A bare number is seconds (recorded behavior).
        for text in ["", "4d", "h", "-1h", "1m1h"]:
            with self.subTest(text=text):
                with self.assertRaises(k.KoradError):
                    k.parse_duration(text)


class TzCase(unittest.TestCase):
    TZ = "Europe/Warsaw"

    def setUp(self):
        self._old_tz = os.environ.get("TZ")
        os.environ["TZ"] = self.TZ
        time.tzset()

    def tearDown(self):
        if self._old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._old_tz
        time.tzset()

    @staticmethod
    def local(*a):
        return dt.datetime(*a).astimezone()


class NextLocalTime(TzCase):
    def test_later_same_day(self):
        t = k.next_local_time("10:00", self.local(2026, 9, 29, 4, 0))
        self.assertEqual((t.day, t.hour, t.minute), (29, 10, 0))
        self.assertEqual(t.utcoffset(), dt.timedelta(hours=2))

    def test_after_the_hour_goes_to_next_day(self):
        t = k.next_local_time("10:00", self.local(2026, 9, 29, 11, 0))
        self.assertEqual((t.day, t.hour), (30, 10))

    def test_exactly_now_goes_to_next_day(self):
        t = k.next_local_time("10:00", self.local(2026, 9, 29, 10, 0))
        self.assertEqual((t.day, t.hour), (30, 10))

    def test_dst_crossing_overnight(self):
        # 2026-10-25 03:00 CEST -> 02:00 CET. From Sat 23:00 (+02:00), 10:00 is Sunday +01:00.
        t = k.next_local_time("10:00", self.local(2026, 10, 24, 23, 0))
        self.assertEqual((t.day, t.hour), (25, 10))
        self.assertEqual(t.utcoffset(), dt.timedelta(hours=1))

    def test_dst_crossing_same_day(self):
        # Regression: next_local_time keeps now's fixed offset when the target is later the
        # same day. At 01:00 +02:00 on 2026-10-25, 10:00 local is +01:00, but the code
        # returns 10:00 +02:00, which is 09:00 local: the guard expires an hour early.
        t = k.next_local_time("10:00", self.local(2026, 10, 25, 1, 0))
        self.assertEqual(t.utcoffset(), dt.timedelta(hours=1))
        self.assertEqual(t.astimezone().hour, 10)

    def test_bad_input(self):
        for text in ["24:00", "10:60", "1000", "", "ten"]:
            with self.subTest(text=text):
                with self.assertRaises(k.KoradError):
                    k.next_local_time(text)


class FmtLocal(TzCase):
    def test_shape(self):
        s = k.fmt_local(self.local(2026, 9, 29, 4, 5, 6))
        self.assertEqual(s, "2026-09-29 04:05:06 CEST (+02:00)")

    def test_winter(self):
        s = k.fmt_local(dt.datetime(2026, 12, 1, 12, 0, tzinfo=UTC))
        self.assertEqual(s, "2026-12-01 13:00:00 CET (+01:00)")

    def test_delta(self):
        self.assertEqual(k.fmt_delta(3 * 3600 + 5 * 60), "3h05m")
        self.assertEqual(k.fmt_delta(-65), "1m05s")


# ---------------------------------------------------------------- guard logic

class GuardViolations(unittest.TestCase):
    def test_no_guard_or_expired(self):
        self.assertEqual(k.guard_violations(None, "series", sp(v2=3100)), [])
        g = guard({"all": {"max_cv": 100}}, hours=-1)
        self.assertEqual(k.guard_violations(g, "independent", sp(v1=3100)), [])

    def test_global_applies_to_both(self):
        g = guard({"all": {"max_cv": 330, "max_ma": 100}})
        self.assertEqual(k.guard_violations(g, "independent", sp(330, 100, 330, 100)), [])
        self.assertEqual(len(k.guard_violations(g, "independent", sp(331, 100, 330, 101))), 2)

    def test_channel_entry_takes_precedence_over_global(self):
        # Recorded behavior: a channel's own limit wins over the global one, per quantity,
        # even when it is looser.
        g = guard({"all": {"max_cv": 330, "max_ma": 100}, "1": {"max_cv": 500}})
        self.assertEqual(k.guard_violations(g, "independent", sp(v1=500)), [])
        self.assertTrue(k.guard_violations(g, "independent", sp(v1=501)))
        self.assertTrue(k.guard_violations(g, "independent", sp(v2=331)))
        self.assertTrue(k.guard_violations(g, "independent", sp(i1=101)))  # current from global

    def test_per_channel_only(self):
        g = guard({"2": {"max_cv": 330}})
        self.assertEqual(k.guard_violations(g, "independent", sp(v1=3100, v2=330)), [])
        self.assertTrue(k.guard_violations(g, "independent", sp(v2=331)))

    def test_mode_not_allowed(self):
        g = guard({"all": {"max_cv": 3100}})
        out = k.guard_violations(g, "series", sp(v1=100, v2=100))
        self.assertTrue(any("mode series" in x for x in out))

    def test_series_counts_twice_vset2(self):
        g = guard({"all": {"max_cv": 3500}}, mode="series")
        self.assertEqual(k.guard_violations(g, "series", sp(v1=1750, v2=1750)), [])
        out = k.guard_violations(g, "series", sp(v1=1751, v2=1751))
        self.assertEqual(len(out), 1)
        self.assertIn("35.02 V", out[0])

    def test_parallel_counts_twice_iset2(self):
        g = guard({"all": {"max_ma": 6000}}, mode="parallel")
        self.assertEqual(k.guard_violations(g, "parallel", sp(i1=3000, i2=3000)), [])
        self.assertTrue(k.guard_violations(g, "parallel", sp(i1=3001, i2=3001)))

    def test_independent_allowed_under_series_guard(self):
        g = guard({"all": {"max_cv": 3500}}, mode="series")
        self.assertEqual(k.guard_violations(g, "independent", sp(v1=3100, v2=3100)), [])
        self.assertTrue(k.guard_violations(g, "parallel", sp()))


class ValidateGuard(unittest.TestCase):
    def ok(self, g):
        k.validate_guard(g)

    def bad(self, g, fragment):
        with self.assertRaises(k.KoradError) as cm:
            k.validate_guard(g)
        self.assertIn(fragment, cm.exception.message)

    def test_valid(self):
        self.ok(guard({"all": {"max_cv": 330}}))
        self.ok(guard({"1": {"max_cv": 330}, "2": {"max_ma": 50}}))
        self.ok(guard({"all": {"max_cv": 4800, "max_ma": 100}}, mode="series"))
        self.ok(guard({"all": {"max_ma": 8000}}, mode="parallel"))

    def test_empty_limits(self):
        self.bad(guard({}), "at least one limit")

    def test_series_needs_global(self):
        self.bad(guard({"2": {"max_cv": 4800}}, mode="series"), "global limits")

    def test_series_at_or_below_single_channel(self):
        self.bad(guard({"all": {"max_cv": 330}}, mode="series"), "makes no sense")
        self.bad(guard({"all": {"max_cv": 3100}}, mode="series"), "makes no sense")

    def test_parallel_at_or_below_single_channel(self):
        self.bad(guard({"all": {"max_ma": 5100}}, mode="parallel"), "makes no sense")

    def test_above_mode_ceiling(self):
        self.bad(guard({"all": {"max_cv": 3200}}), "above what")
        self.bad(guard({"all": {"max_cv": 6300}}, mode="series"), "above what")
        self.bad(guard({"all": {"max_ma": 10300}}, mode="parallel"), "above what")


class GuardLoosening(unittest.TestCase):
    def setUp(self):
        self.old = guard({"all": {"max_cv": 330, "max_ma": 100}}, hours=4)

    def with_(self, **kw):
        g = json.loads(json.dumps(self.old))
        g.update(kw)
        return g

    def test_no_active_guard(self):
        self.assertEqual(k.guard_loosening(None, self.old), [])
        expired = guard({"all": {"max_cv": 100}}, hours=-1)
        self.assertEqual(k.guard_loosening(expired, None), [])

    def test_tighten(self):
        new = self.with_(limits={"all": {"max_cv": 250, "max_ma": 50}})
        self.assertEqual(k.guard_loosening(self.old, new), [])
        later = self.with_(expires_utc=(dt.datetime.now(UTC) + dt.timedelta(hours=8)).isoformat())
        self.assertEqual(k.guard_loosening(self.old, later), [])

    def test_raise_voltage(self):
        new = self.with_(limits={"all": {"max_cv": 1200, "max_ma": 100}})
        self.assertEqual(k.guard_loosening(self.old, new), ["12.00"])

    def test_raise_current(self):
        new = self.with_(limits={"all": {"max_cv": 330, "max_ma": 500}})
        self.assertEqual(k.guard_loosening(self.old, new), ["0.500"])

    def test_removed_limit(self):
        new = self.with_(limits={"all": {"max_cv": 330}})
        self.assertEqual(k.guard_loosening(self.old, new), ["unlimited"])

    def test_added_mode(self):
        old = guard({"all": {"max_cv": 4800}}, hours=4)
        new = dict(old, mode="series")
        self.assertEqual(k.guard_loosening(old, new), ["series"])

    def test_earlier_expiry(self):
        exp = dt.datetime.now(UTC) + dt.timedelta(hours=1)
        new = self.with_(expires_utc=exp.isoformat())
        self.assertEqual(k.guard_loosening(self.old, new), [f"{exp.astimezone():%H:%M}"])

    def test_clear(self):
        self.assertEqual(k.guard_loosening(self.old, None), ["clear"])

    def test_several_words(self):
        new = self.with_(limits={"all": {"max_cv": 1200}})
        self.assertEqual(k.guard_loosening(self.old, new), ["12.00", "unlimited"])


# ---------------------------------------------------------------- fake + protocol

class FakeFidelity(unittest.TestCase):
    def setUp(self):
        self.f = k.FakeSerial()

    def w(self, cmd):
        self.f.write(cmd.encode())
        time.sleep(0.03)

    def q(self, cmd, n=5):
        self.f.reset_input_buffer()
        self.f.write(cmd.encode())
        return self.f.read(n).decode()

    def test_three_decimals_ignored(self):
        self.w("VSET2:5")
        self.w("VSET2:5.123")
        self.assertEqual(self.q("VSET2?"), "05.00")

    def test_over_31_ignored_31_accepted(self):
        self.w("VSET2:31.00")
        self.assertEqual(self.q("VSET2?"), "31.00")
        self.w("VSET2:35")
        self.assertEqual(self.q("VSET2?"), "31.00")
        self.w("ISET2:5.100")
        self.w("ISET2:6")
        self.assertEqual(self.q("ISET2?"), "5.100")

    def test_command_within_20ms_of_write_is_lost(self):
        self.w("VSET2:1.00")
        self.f.write(b"VSET1:2.00")
        self.f.write(b"VSET2:3.00")      # immediately after a write: lost
        time.sleep(0.03)
        self.assertEqual(self.q("VSET2?"), "01.00")
        self.assertIn("VSET2:3.00", self.f.lost)

    def test_track_copies_ch2_and_turns_out_off(self):
        self.w("VSET1:2.00")
        self.w("VSET2:6.00")
        self.w("OUT1")
        self.w("TRACK1")
        self.assertEqual(self.q("VSET1?"), "06.00")
        st = k.decode_status(ord(self.q("STATUS?", 1)))
        self.assertEqual((st["mode"], st["output"]), ("series", False))
        self.w("TRACK0")
        self.assertEqual(self.q("VSET1?"), "06.00")   # not restored

    def test_rcl_turns_out_off(self):
        self.f.mem[2] = (250, 350, 1200, 2000)
        self.w("OUT1")
        self.w("RCL2")
        st = k.decode_status(ord(self.q("STATUS?", 1)))
        self.assertFalse(st["output"])
        self.assertEqual(self.q("VSET2?"), "12.00")

    def test_unknown_command_silent(self):
        self.assertEqual(self.q("FOO?"), "")
        self.assertEqual(self.q("VSET3?"), "")


class KoradProtocol(unittest.TestCase):
    def setUp(self):
        self.f = k.FakeSerial()
        self.d = k.Korad(self.f)

    def drop(self, prefix, times):
        orig = self.f.write
        left = {"n": times}

        def write(data):
            if data.decode().startswith(prefix) and left["n"] != 0:
                left["n"] -= 1
                return len(data)
            return orig(data)
        self.f.write = write

    def test_set_and_readback(self):
        self.assertEqual(self.d.set_v(2, 330), 330)
        self.assertEqual(self.d.set_i(2, 50), 50)
        self.assertEqual(self.d.setpoints()["v2"], 330)

    def test_back_to_back_writes_survive_gap(self):
        self.d.set_v(1, 100)
        self.d.set_v(2, 200)
        self.assertEqual(self.f.lost, [])

    def test_retry_once(self):
        self.drop("VSET1:", 1)
        self.assertEqual(self.d.set_v(1, 500), 500)

    def test_verify_error(self):
        self.drop("ISET2:", -1)
        with self.assertRaises(k.KoradError) as cm:
            self.d.set_i(2, 77)
        self.assertEqual(cm.exception.code, "E_VERIFY")

    def test_output_and_mode(self):
        self.assertTrue(self.d.set_output(True)["output"])
        self.assertEqual(self.d.set_mode("parallel")["mode"], "parallel")
        self.assertFalse(self.d.status()["output"])

    def test_query_without_reply(self):
        with self.assertRaises(k.KoradError) as cm:
            self.d.query("FOO?", 5)
        self.assertEqual(cm.exception.code, "E_DEVICE")


# ---------------------------------------------------------------- daemon e2e

class DaemonCase(unittest.TestCase):
    CFG = {"poll_hz": 50, "setpoint_every_s": 0.05}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = {x: os.environ.get(x) for x in ("KORAD_HOME", "KORAD_SOCKET")}
        os.environ["KORAD_HOME"] = self.tmp.name
        os.environ.pop("KORAD_SOCKET", None)
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()          # the daemon logs events to stdout
        cfg = dict(k.DEFAULT_CONFIG, **self.CFG)
        self.daemon = k.Daemon(cfg, fake=True)
        self.thread = threading.Thread(target=self.daemon.serve, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while True:
            try:
                self.c = k.Client(name="test")
            except k.KoradError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
                continue
            try:
                self.c.call("ping")
                break
            except k.KoradError:
                self.c.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        self.fake = self.daemon.dev.port

    def tearDown(self):
        try:
            if not self.daemon.stopping.is_set():
                self.c.call("shutdown")
        except k.KoradError:
            pass
        self.thread.join(timeout=10)
        self.c.close()
        sys.stdout = self._stdout
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.tmp.cleanup()

    def set_guard(self, g, confirm=None):
        return self.c.call("guard_set", guard=g, confirm=confirm)

    def err(self, op, **kw):
        with self.assertRaises(k.KoradError) as cm:
            self.c.call(op, **kw)
        return cm.exception

    def wait_for(self, pred, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return False

    def events(self, kind=None):
        ev = self.c.call("state")["events"]
        return [e for e in ev if kind is None or e["kind"] == kind]


class DaemonE2E(DaemonCase):
    def test_set_without_guard_warns_on_increase_only(self):
        r = self.c.call("set", channel="2", v=330, i=50)
        self.assertEqual((r["after"]["v2"], r["after"]["i2"]), (330, 50))
        self.assertEqual(self.fake.v[2], 330)
        # The fake starts at 30 V; 3.3 V is a decrease but the current rose 10 -> 50 mA.
        self.assertTrue(self.c.last_warnings)
        self.c.call("set", channel="2", v=100)
        self.assertEqual(self.c.last_warnings, [])
        self.c.call("set", channel="2", v=200)
        self.assertTrue(any("no guard" in w for w in self.c.last_warnings))

    def test_set_both_channels(self):
        self.c.call("set", channel="12", v=500, i=20)
        self.assertEqual((self.fake.v[1], self.fake.v[2], self.fake.i[1], self.fake.i[2]),
                         (500, 500, 20, 20))

    def test_nothing_to_set(self):
        self.assertEqual(self.err("set", channel="1").code, "E_USAGE")

    def test_set_refused_by_guard(self):
        self.c.call("set", channel="12", v=100)
        self.set_guard(guard({"2": {"max_cv": 500}}))
        e = self.err("set", channel="2", v=600)
        self.assertEqual(e.code, "E_GUARD")
        self.assertEqual(self.fake.v[2], 100)          # nothing written
        self.c.call("set", channel="2", v=500)
        self.assertEqual(self.c.last_warnings, [])

    def test_out_refused_by_guard(self):
        self.set_guard(guard({"all": {"max_cv": 500}}))   # fake starts at 30 V
        self.assertEqual(self.err("out", on=True).code, "E_GUARD")
        self.assertFalse(self.fake.out)
        self.c.call("set", channel="12", v=330)
        self.assertTrue(self.c.call("out", on=True)["status"]["output"])
        self.assertTrue(self.c.call("out", on=False)["status"]["output"] is False)

    def test_mode_refused_by_guard(self):
        self.set_guard(guard({"all": {"max_cv": 500}}))
        self.assertEqual(self.err("mode", mode="series").code, "E_GUARD")
        self.assertEqual(self.fake.mode, 0)

    def test_mode_series_allowed_by_series_guard(self):
        self.c.call("set", channel="12", v=1000, i=10)
        self.set_guard(guard({"all": {"max_cv": 4000}}, mode="series"))
        r = self.c.call("mode", mode="series")
        self.assertEqual(r["status"]["mode"], "series")
        self.assertEqual(self.err("set", channel="2", v=2100).code, "E_GUARD")   # 42 V total
        self.c.call("set", channel="2", v=2000)
        self.assertEqual(self.err("set", channel="1", v=100).code, "E_USAGE")

    def test_mode_change_without_guard_warns_and_turns_out_off(self):
        self.c.call("set", channel="12", v=100)
        self.c.call("out", on=True)
        r = self.c.call("mode", mode="parallel")
        self.assertTrue(any("no guard" in w for w in self.c.last_warnings))
        self.assertFalse(r["status"]["output"])
        self.assertFalse(self.fake.out)

    def test_live_violation_turns_output_off(self):
        self.c.call("set", channel="12", v=330, i=50)
        self.set_guard(guard({"all": {"max_cv": 500}}))
        self.c.call("out", on=True)
        with self.fake.lock:
            self.fake.v[2] = 1200                     # someone turns the knob
        self.assertTrue(self.wait_for(lambda: not self.fake.out), "output stayed ON")
        self.assertTrue(self.wait_for(lambda: any("turned OFF" in e["message"]
                                                  for e in self.events("guard"))))
        self.assertTrue(self.events("panel"))

    def test_preset_over_guard_zeroes_setpoints(self):
        self.fake.mem[3] = (3100, 5100, 3100, 5100)
        self.c.call("set", channel="12", v=330, i=50)
        self.set_guard(guard({"all": {"max_cv": 500}}))
        self.c.call("out", on=True)
        e = self.err("preset", n=3)
        self.assertEqual(e.code, "E_GUARD")
        self.assertEqual((self.fake.v[1], self.fake.v[2], self.fake.out), (0, 0, False))

    def test_preset_within_guard(self):
        self.fake.mem[5] = (120, 100, 120, 100)
        self.set_guard(guard({"all": {"max_cv": 500}}))
        r = self.c.call("preset", n=5)
        self.assertEqual(r["setpoints"]["v2"], 120)
        r = self.c.call("preset", n=4, save=True)
        self.assertEqual(self.fake.mem[4], (120, 100, 120, 100))

    def test_confirm_loosen_and_clear(self):
        self.set_guard(guard({"all": {"max_cv": 330}}, hours=4))
        e = self.err("guard_set", guard=guard({"all": {"max_cv": 1200}}, hours=4))
        self.assertEqual(e.code, "E_CONFIRM")
        self.assertNotIn("12.00", e.message)          # the word is not handed back
        self.assertEqual(self.err("guard_set", guard=guard({"all": {"max_cv": 1200}}, hours=4),
                                  confirm="1.00").code, "E_CONFIRM")
        self.assertEqual(self.daemon.guard["limits"]["all"]["max_cv"], 330)
        self.set_guard(guard({"all": {"max_cv": 1200}}, hours=4), confirm="12.00")
        self.assertEqual(self.daemon.guard["limits"]["all"]["max_cv"], 1200)
        self.assertEqual(self.err("guard_clear").code, "E_CONFIRM")
        self.assertIsNotNone(self.daemon.guard)
        self.c.call("guard_clear", confirm="clear")
        self.assertIsNone(self.daemon.guard)
        self.assertFalse((Path(self.tmp.name) / "state" / "guard.json").exists())

    def test_tighten_needs_no_confirm(self):
        self.set_guard(guard({"all": {"max_cv": 1200}}, hours=4))
        self.set_guard(guard({"all": {"max_cv": 330}}, hours=5))
        self.assertEqual(self.daemon.guard["limits"]["all"]["max_cv"], 330)

    def test_guard_persists_to_file(self):
        g = guard({"1": {"max_cv": 330}})
        self.set_guard(g)
        on_disk = json.loads((Path(self.tmp.name) / "state" / "guard.json").read_text())
        self.assertEqual(on_disk["limits"], g["limits"])

    def test_invalid_guard_refused(self):
        self.assertEqual(self.err("guard_set", guard=guard({"all": {"max_cv": 330}},
                                                            mode="series")).code, "E_USAGE")

    def test_audit_records_refusal(self):
        self.set_guard(guard({"all": {"max_cv": 500}}))
        self.err("set", channel="1", v=900)
        lines = [json.loads(x) for x in
                 (Path(self.tmp.name) / "state" / "audit.jsonl").read_text().splitlines()]
        ref = [x for x in lines if x["kind"] == "refused"]
        self.assertTrue(ref)
        self.assertEqual(ref[-1]["request"]["v"], 900)
        self.assertEqual(ref[-1]["source"], "test")

    def test_events_carry_increasing_seq(self):
        self.c.call("set", channel="1", v=100)
        self.c.call("set", channel="1", v=200)
        ev = self.events()
        seqs = [e["seq"] for e in ev]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))
        last = self.c.call("state")["last_event_seq"]
        self.c.call("set", channel="1", v=300)
        newer = self.c.call("state", events_after=last)["events"]
        self.assertTrue(newer and all(e["seq"] > last for e in newer))

    def test_wait_sample_advances(self):
        s = self.c.call("state")
        s2 = self.c.call("wait_sample", after=s["seq"], timeout=2)
        self.assertGreater(s2["seq"], s["seq"])
        self.assertIsNotNone(s2["measured"])

    def test_ovp_ocp(self):
        self.assertTrue(self.c.call("bit", which="ovp", on=True)["status"]["ovp"])
        self.assertTrue(self.c.call("bit", which="ocp", on=True)["status"]["ocp"])
        self.assertFalse(self.c.call("bit", which="ocp", on=False)["status"]["ocp"])
        self.assertEqual(self.err("bit", which="beep", on=True).code, "E_USAGE")

    def test_unknown_op(self):
        self.assertEqual(self.err("explode").code, "E_USAGE")

    def test_safe_stop(self):
        self.c.call("mode", mode="series")
        self.c.call("set", channel="2", v=1200, i=500)
        self.c.call("out", on=True)
        self.c.call("shutdown")
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive())
        f = self.fake
        self.assertEqual((f.out, f.mode, f.v[1], f.v[2], f.i[1], f.i[2]), (False, 0, 0, 0, 10, 10))
        self.assertTrue(any(e["kind"] == "stop" for e in self.daemon.events))
        self.assertFalse(k.socket_path().exists())

    def test_off_has_priority_over_queued_commands(self):
        gate, order = threading.Event(), []
        blocker = threading.Thread(target=self.daemon.submit,
                                   args=(lambda: gate.wait(5),), daemon=True)
        blocker.start()
        time.sleep(0.2)                       # the device thread is now inside the blocker
        t1 = threading.Thread(target=self.daemon.submit,
                              args=(lambda: order.append("cmd"),), daemon=True)
        t1.start()
        time.sleep(0.1)
        t2 = threading.Thread(target=self.daemon.submit,
                              args=(lambda: order.append("off"), self.daemon.PRIO_OFF), daemon=True)
        t2.start()
        time.sleep(0.1)
        gate.set()
        for t in (blocker, t1, t2):
            t.join(timeout=5)
        self.assertEqual(order, ["off", "cmd"])


class DaemonNoGuardStart(DaemonCase):
    def test_start_event_warns(self):
        self.assertTrue(any(e["kind"] == "warning" and "no guard" in e["message"]
                            for e in self.daemon.events))


class DaemonLiveWarn(DaemonCase):
    CFG = dict(DaemonCase.CFG, on_live_violation="warn", on_recall_violation="warn")

    def test_live_violation_warn_only(self):
        self.c.call("set", channel="12", v=330)
        self.set_guard(guard({"all": {"max_cv": 500}}))
        self.c.call("out", on=True)
        with self.fake.lock:
            self.fake.v[1] = 1200
        self.assertTrue(self.wait_for(lambda: any("WARNING" in e["message"]
                                                  for e in self.events("guard"))))
        self.assertTrue(self.fake.out)

    def test_recall_violation_warn_only(self):
        self.fake.mem[3] = (3100, 100, 3100, 100)
        self.set_guard(guard({"all": {"max_cv": 500}}))
        r = self.c.call("preset", n=3)
        self.assertEqual(r["setpoints"]["v1"], 3100)


# ---------------------------------------------------------------- CLI

HAS_CLI = "def main" in (HERE / "korad.py").read_text()


@unittest.skipUnless(HAS_CLI, "korad.py has no main() yet (the CLI is being written)")
class Cli(DaemonCase):
    def run_cli(self, *args, stdin=""):
        env = dict(os.environ)
        return subprocess.run([sys.executable, str(HERE / "korad.py"), *args], input=stdin,
                              capture_output=True, text=True, env=env, timeout=30)

    def test_usage_error_exit_2(self):
        r = self.run_cli("set", "1", "-v", "3v3")
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_guard_refusal_exit_3_json(self):
        self.set_guard(guard({"all": {"max_cv": 500}}))
        r = self.run_cli("--json", "set", "1", "-v", "9")
        self.assertEqual(r.returncode, 3, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        env = json.loads(lines[0])
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "E_GUARD")
        self.assertIn("command", env)

    def test_confirm_exit_6(self):
        self.set_guard(guard({"all": {"max_cv": 330}}, hours=4))
        r = self.run_cli("guard", "clear")
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertIsNotNone(self.daemon.guard)

    def test_status_json_ok(self):
        r = self.run_cli("--json", "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        env = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(env["ok"])
        self.assertIn("data", env)
        self.assertIn("warnings", env)



# ---------------------------------------------------------------- design-review regressions

import argparse  # noqa: E402

import monitor  # noqa: E402


class ReviewUnits(unittest.TestCase):
    def test_r10_unit_confusion_hint(self):
        for text, kind, hint in (("50", "i", "did you mean 50mA?"), ("5000", "v", "did you mean 5000mV?")):
            with self.assertRaises(k.KoradError) as cm:
                k.parse_value(text, kind)
            self.assertIn(hint, cm.exception.message)

    def test_r10_no_hint_with_a_unit_or_when_mv_is_still_invalid(self):
        with self.assertRaises(k.KoradError) as cm:
            k.parse_value("50A", "i")
        self.assertNotIn("did you mean", cm.exception.message)
        with self.assertRaises(k.KoradError) as cm:
            k.parse_value("99999", "v")          # 99.999 V is finer than 10 mV and too high
        self.assertNotIn("did you mean", cm.exception.message)

    def test_r3_guard_duration_needs_a_unit(self):
        with self.assertRaises(k.KoradError) as cm:
            k.parse_guard_duration("4")
        self.assertIn("no unit", cm.exception.message)
        self.assertEqual(k.parse_guard_duration("4h"), 4 * 3600)
        self.assertEqual(k.parse_duration("4"), 4.0)   # elsewhere a bare number stays seconds

    def test_r3_guard_window_minimum_and_past(self):
        for hours in (-1, 30 / 3600):
            with self.assertRaises(k.KoradError):
                k.validate_guard_window(guard({"all": {"max_cv": 330}}, hours=hours))
        k.validate_guard_window(guard({"all": {"max_cv": 330}}, hours=2 / 60))

    def test_r6_confirm_word_format_without_the_word(self):
        self.assertIn("2 decimals", k._word_kind("12.00"))
        self.assertIn("3 decimals", k._word_kind("0.500"))
        self.assertIn("HH:MM", k._word_kind("18:00"))
        for w in ("12.00", "0.500", "18:00"):
            self.assertNotIn(w, k._word_kind(w))

    def test_r11_today_under_one_hour_warns(self):
        soon = (dt.datetime.now() + dt.timedelta(minutes=30)).strftime("%H:%M")
        out = k.Out(False, "guard")
        a = argparse.Namespace(for_=None, until=None, today=True, _out=out)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            exp = k._expiry(a, dict(k.DEFAULT_CONFIG, today_ends=soon))
        self.assertLess((exp - k.now_local()).total_seconds(), 3600)
        self.assertIn("--today ends at", err.getvalue())


class ReviewMonitorKeys(unittest.TestCase):
    def test_r5_double_press_fires_after_settle(self):
        st = monitor.UIState(setpoints=sp())
        self.assertEqual(monitor.handle_key(st, "y", 100.0), [])
        self.assertEqual(monitor.handle_key(st, "y", 100.4), [])
        self.assertEqual(monitor.tick(st, 100.45), [])
        self.assertEqual(monitor.tick(st, 100.53), [("out", True)])
        self.assertEqual(monitor.tick(st, 100.6), [])

    def test_r5_auto_repeat_never_turns_on(self):
        st = monitor.UIState(setpoints=sp())
        t = 100.0
        acts = monitor.handle_key(st, "y", t)
        t += 0.03
        acts += monitor.handle_key(st, "y", t)            # fast repeat
        self.assertEqual(acts + monitor.tick(st, t + 0.5), [])

    def test_r5_held_key_with_slow_first_repeat_never_turns_on(self):
        st = monitor.UIState(setpoints=sp())
        acts = monitor.handle_key(st, "t", 100.0)
        acts += monitor.handle_key(st, "t", 100.4)        # first repeat after the repeat delay
        for n in range(1, 20):                            # then the repeat rate
            acts += monitor.handle_key(st, "t", 100.4 + n * 0.033)
            acts += monitor.tick(st, 100.4 + n * 0.033)
        acts += monitor.tick(st, 102.0)
        self.assertNotIn(("out", True), acts)

    def test_r11_coarse_key_step_capped(self):
        st = monitor.UIState(setpoints=sp(v1=100), step=2)
        self.assertEqual(monitor.handle_key(st, "K", 0.0), [("set", 1, "v", 600)])
        st = monitor.UIState(setpoints=sp(i1=10), step=2, field="i")
        self.assertEqual(monitor.handle_key(st, "D", 0.0), [("set", 1, "i", 510)])


class ReviewDaemon(DaemonCase):
    def test_r1_flood_does_not_starve_live_check(self):
        self.c.call("set", channel="12", v=300, i=50)
        self.set_guard(guard({"all": {"max_cv": 500}}))
        self.c.call("out", on=True)
        stop = threading.Event()

        def flood():
            c = k.Client(name="flood")
            try:
                while not stop.is_set():
                    c.call("bit", which="ovp", on=False)
            except k.KoradError:
                pass
            finally:
                c.close()
        threads = [threading.Thread(target=flood, daemon=True) for _ in range(3)]
        for th in threads:
            th.start()
        time.sleep(0.3)
        with self.fake.lock:
            self.fake.v[2] = 2400                        # front-panel knob
        t0 = time.monotonic()
        off = self.wait_for(lambda: not self.fake.out, timeout=3)
        took = time.monotonic() - t0
        stop.set()
        for th in threads:
            th.join(timeout=5)
        self.assertTrue(off, "output stayed ON under a command flood")
        self.assertLess(took, 1.5)

    def test_r2_leaving_series_is_never_refused(self):
        self.c.call("set", channel="2", v=500, i=100)
        self.c.call("mode", mode="series")
        self.set_guard(guard({"1": {"max_cv": 330}, "2": {"max_cv": 1200}}))
        r = self.c.call("mode", mode="independent")
        self.assertEqual(r["status"]["mode"], "independent")
        self.assertTrue(any("break the guard" in w for w in self.c.last_warnings))
        e = self.err("out", on=True)
        self.assertEqual(e.code, "E_GUARD")

    def test_r3_daemon_refuses_expired_guard(self):
        e = self.err("guard_set", guard=guard({"all": {"max_cv": 330}}, hours=-0.1))
        self.assertEqual(e.code, "E_USAGE")
        self.assertIsNone(self.daemon.guard)

    def test_r4_partial_set_reported_and_audited(self):
        real_write = self.fake.write

        def drop_vset2(data):
            if data.startswith(b"VSET2:"):
                return len(data)
            return real_write(data)
        self.fake.write = drop_vset2
        e = self.err("set", channel="2", v=500, i=900)
        self.assertEqual(e.code, "E_VERIFY")
        self.assertIn("ISET2=0.900 A", e.message)
        self.assertIn("Read-back now", e.message)
        self.assertEqual(self.daemon.cache["setpoints"]["i2"], 900)
        self.assertTrue(self.events("set-partial"))
        time.sleep(0.3)
        self.assertFalse([ev for ev in self.events("panel") if "setpoints" in ev["message"]])
        audit = (Path(self.tmp.name) / "state" / "audit.jsonl").read_text()
        self.assertIn('"kind": "set-partial"', audit)

    def test_r6_confirm_error_states_format(self):
        self.set_guard(guard({"all": {"max_cv": 330}}))
        new = guard({"all": {"max_cv": 1200}})
        e = self.err("guard_set", guard=new, confirm="12")
        self.assertEqual(e.code, "E_CONFIRM")
        self.assertIn("2 decimals", e.message)
        self.assertNotIn("12.00", e.message)

    def test_r8_out_off_with_device_absent_says_output_may_be_on(self):
        def fail_open():
            raise OSError("gone")
        self.daemon._open = fail_open
        self.daemon.dev = None
        self.daemon._next_open = 0
        e = self.err("out", on=False)
        self.assertEqual(e.code, "E_DEVICE")
        self.assertIn("may still be ON", e.message)

    def test_r9_refusal_names_the_other_channel_fix(self):
        self.set_guard(guard({"all": {"max_cv": 500}}))
        # the fake starts with CH1 at 30.00 V
        e = self.err("set", channel="2", v=300)
        self.assertIn("next step: lower CH1 first: korad set 1 -v 5.00", e.message)

    def test_s1_out_on_refused_when_other_channel_unlimited(self):
        self.c.call("set", channel="2", v=330, i=50)
        self.set_guard(guard({"2": {"max_cv": 500}}))
        e = self.err("out", on=True)
        self.assertEqual(e.code, "E_GUARD")
        self.assertIn("CH1 at 30.00 V has no limit", e.message)
        self.c.call("set", channel="1", v=0)
        self.c.call("out", on=True)
        self.assertTrue(self.fake.out)

    def test_s1_live_unlimited_channel_turns_output_off(self):
        self.c.call("set", channel="1", v=0)
        self.c.call("set", channel="2", v=330, i=50)
        self.set_guard(guard({"2": {"max_cv": 500}}))
        self.c.call("out", on=True)
        with self.fake.lock:
            self.fake.v[1] = 1200
        self.assertTrue(self.wait_for(lambda: not self.fake.out, timeout=2))

    def test_s1_current_only_guard_counts_as_a_limit(self):
        self.c.call("set", channel="12", v=330, i=50)
        self.set_guard(guard({"all": {"max_ma": 100}}))
        self.c.call("out", on=True)
        self.assertTrue(self.fake.out)


@unittest.skipUnless(HAS_CLI, "korad.py has no main() yet")
class ReviewCli(DaemonCase):
    run_cli = Cli.run_cli

    def test_r3_cli_bare_for_refused(self):
        r = self.run_cli("guard", "set", "-v", "3.3", "--for", "4")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("no unit", r.stderr)
        r = self.run_cli("guard", "set", "-v", "3.3", "--for", "30s")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIsNone(self.daemon.guard)

    def test_r10_cli_hint(self):
        r = self.run_cli("set", "2", "-i", "50")
        self.assertEqual(r.returncode, 2)
        self.assertIn("did you mean 50mA?", r.stderr)

    def test_s2_guard_show_none_once(self):
        r = self.run_cli("guard")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.count("NONE"), 1, r.stdout)
        self.assertEqual(r.stderr.strip(), "", r.stderr)

    def test_r7_daemon_stop_ok_matches_pid(self):
        r = self.run_cli("--json", "daemon", "stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        env = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(env["data"]["stop_event"]["pid"], os.getpid())

    def test_r7_daemon_stop_failed_exit_5(self):
        def fail_open():
            raise OSError("gone")
        self.daemon._open = fail_open
        self.daemon.dev = None
        self.daemon._next_open = 0
        r = self.run_cli("daemon", "stop")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("may still be ON", r.stderr)



# ---------------------------------------------------------------- live-test regressions

import errno  # noqa: E402
import fcntl  # noqa: E402
import socket  # noqa: E402


def _err(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except k.KoradError as e:
        return e
    raise AssertionError(f"{fn.__name__}{args} did not raise KoradError")


class LiveUnits(unittest.TestCase):
    def test_l20_unicode_digits_refused(self):
        for fn, arg in [(lambda s: k.parse_value(s, "v"), "３.３"), (lambda s: k.parse_value(s, "v"), "٣.٣"),
                        (lambda s: k.parse_value(s, "i"), "５0mA"), (k.parse_duration, "１h"),
                        (k.parse_guard_duration, "１h"), (k.next_local_time, "٠٩:٠٠"),
                        (k._preset_n, "٥"), (k._channel, "１"), (k.check_poll, "５")]:
            with self.subTest(arg=arg):
                self.assertEqual(_err(fn, arg).code, "E_USAGE")

    def test_l20_mega_units_refused(self):
        for text, kind in [("3300MV", "v"), ("5Mv", "v"), ("50MA", "i"), ("50Ma", "i")]:
            with self.subTest(text=text):
                e = _err(k.parse_value, text, kind)
                self.assertIn("mega", e.message)

    def test_l20_space_before_unit_ok_control_chars_refused(self):
        self.assertEqual(k.parse_value("3.3 V", "v"), 330)
        self.assertEqual(k.parse_value(" 50 mA ", "i"), 50)
        for text in ("3.3\n", "\t3.3", "3.3\r", "+3.3", "3,3"):
            with self.subTest(text=repr(text)):
                self.assertEqual(_err(k.parse_value, text, "v").code, "E_USAGE")

    def test_l20_guard_for_capped_at_30_days(self):
        self.assertEqual(k.parse_guard_duration("720h"), 720 * 3600)
        self.assertIn("30 days", _err(k.parse_guard_duration, "721h").message)
        self.assertEqual(_err(k.parse_guard_duration, "99999999h").code, "E_USAGE")

    def test_l4_poll_validation(self):
        for bad in (0.001, 0.5, "0.5", "nan", "inf", float("nan"), float("inf"), True, "1e3", "", -5):
            with self.subTest(bad=bad):
                self.assertEqual(_err(k.check_poll, bad).code, "E_USAGE")
        for good, want in (("max", "max"), (" MAX ", "max"), ("1", 1.0), (1, 1), (50, 50), ("7.5", 7.5)):
            self.assertEqual(k.check_poll(good), want)
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "config.toml"
            cfgp.write_text("poll_hz = 0.5\n")
            self.assertEqual(_err(k.load_config, cfgp).code, "E_USAGE")

    def test_l32_guard_shape(self):
        good = guard({"all": {"max_cv": 500, "max_ma": 50}})
        self.assertIs(k.check_guard_shape(good), good)
        written = dict(good, version=1, set_at_utc=dt.datetime.now(UTC).isoformat())  # as the CLI writes it
        self.assertIs(k.check_guard_shape(written), written)
        naive = dict(good, expires_utc=dt.datetime.now().replace(tzinfo=None).isoformat())
        bads = [naive, dict(good, limits={"all": {"max_cv": "5"}}), {k2: v for k2, v in good.items() if k2 != "limits"},
                dict(good, limits={"3": {"max_cv": 5}}), dict(good, extra=1), dict(good, mode="turbo"),
                dict(good, limits={"all": {"max_cv": True}}), dict(good, expires_utc="soon"), [good]]
        for bad in bads:
            with self.subTest(bad=bad):
                self.assertEqual(_err(k.check_guard_shape, bad).code, "E_USAGE")

    @unittest.skipIf(os.geteuid() == 0, "root ignores TIOCEXCL")
    def test_l11_tiocexcl_blocks_a_second_open(self):
        master, slave = os.openpty()
        try:
            name = os.ttyname(slave)
            k.set_exclusive(slave)
            with self.assertRaises(OSError) as cm:
                os.close(os.open(name, os.O_RDWR | os.O_NOCTTY))
            self.assertEqual(cm.exception.errno, errno.EBUSY)
        finally:
            os.close(master)
            os.close(slave)

    @unittest.skipIf(os.geteuid() == 0, "root ignores TIOCEXCL")
    def test_l11_busy_port_is_reported_as_busy_not_absent(self):
        try:
            import serial  # noqa: F401
        except ImportError:
            self.skipTest("pyserial not installed")
        master, slave = os.openpty()
        tmp = tempfile.TemporaryDirectory()
        old = os.environ.get("KORAD_HOME")
        os.environ["KORAD_HOME"] = tmp.name
        stdout, sys.stdout = sys.stdout, io.StringIO()
        try:
            k.set_exclusive(slave)
            d = k.Daemon(dict(k.DEFAULT_CONFIG), port=os.ttyname(slave))
            e = _err(d._ensure)
            self.assertIn("port busy", e.message)
            self.assertEqual(d.cache["device"], "busy")
        finally:
            sys.stdout = stdout
            os.close(master)
            os.close(slave)
            if old is None:
                os.environ.pop("KORAD_HOME", None)
            else:
                os.environ["KORAD_HOME"] = old
            tmp.cleanup()


class LiveDaemon(DaemonCase):
    def _block(self, seconds):
        """Hold the device thread for `seconds`; returns the gate to release it early."""
        gate = threading.Event()
        th = threading.Thread(target=self.daemon.submit, args=(lambda: gate.wait(seconds),),
                              kwargs={"timeout": seconds + 5}, daemon=True)
        th.start()
        time.sleep(0.15)
        return gate, th

    def _call_bg(self, op, **kw):
        res = {}

        def run():
            c = k.Client(name=f"bg {op}")
            try:
                res["data"] = c.call(op, **kw)
            except k.KoradError as e:
                res["err"] = e
            finally:
                c.close()
        th = threading.Thread(target=run, daemon=True)
        th.start()
        return res, th

    def test_l1_older_on_dropped_after_newer_off(self):
        self.c.call("set", channel="2", v=300, i=20)
        gate, blk = self._block(5)
        on, t_on = self._call_bg("out", on=True)
        time.sleep(0.15)
        off, t_off = self._call_bg("out", on=False)
        time.sleep(0.15)
        gate.set()
        for th in (blk, t_on, t_off):
            th.join(timeout=10)
        self.assertEqual(on["err"].code, "E_SUPERSEDED")
        self.assertIn("data", off)
        time.sleep(0.2)
        self.assertFalse(self.fake.out)
        self.assertEqual(k.EXIT["E_SUPERSEDED"], 7)

    def test_l1_on_queued_too_long_dropped(self):
        self.c.call("set", channel="2", v=300, i=20)
        gate, blk = self._block(2.6)
        on, t_on = self._call_bg("out", on=True)
        t_on.join(timeout=10)
        blk.join(timeout=10)
        self.assertEqual(on["err"].code, "E_SUPERSEDED")
        self.assertIn("queued too long", on["err"].message)
        self.assertFalse(self.fake.out)

    def test_l1_newer_on_after_off_runs(self):
        self.c.call("set", channel="2", v=300, i=20)
        gate, blk = self._block(5)
        off, t_off = self._call_bg("out", on=False)
        time.sleep(0.15)
        on, t_on = self._call_bg("out", on=True)
        time.sleep(0.15)
        gate.set()
        for th in (blk, t_on, t_off):
            th.join(timeout=10)
        self.assertIn("data", on, on.get("err") and on["err"].message)
        self.assertTrue(self.fake.out)

    def test_l2_request_of_a_dead_client_is_not_run(self):
        gate, blk = self._block(5)
        s = socket.socket(socket.AF_UNIX)
        s.connect(str(k.socket_path()))
        s.sendall((json.dumps({"op": "set", "channel": "2", "v": 123, "client": "dead"}) + "\n").encode())
        time.sleep(0.2)
        s.close()                          # the client dies before the device thread gets to it
        time.sleep(0.1)
        gate.set()
        blk.join(timeout=10)
        time.sleep(0.4)
        self.assertNotEqual(self.fake.v[2], 123)
        self.assertTrue(any("disconnected" in e["message"] for e in self.events("dropped")))

    def test_l2_off_of_a_dead_client_still_runs(self):
        self.c.call("set", channel="2", v=300, i=20)
        self.c.call("out", on=True)
        gate, blk = self._block(5)
        s = socket.socket(socket.AF_UNIX)
        s.connect(str(k.socket_path()))
        s.sendall((json.dumps({"op": "out", "on": False, "client": "dead"}) + "\n").encode())
        time.sleep(0.2)
        s.close()
        gate.set()
        blk.join(timeout=10)
        self.assertTrue(self.wait_for(lambda: not self.fake.out))

    def test_l3_submit_timeout_cancels_the_command(self):
        gate, blk = self._block(5)
        ran = []
        e = _err(self.daemon.submit, lambda: ran.append(1), timeout=0.3)
        self.assertEqual(e.code, "E_DEVICE")
        self.assertIn("NOT run", e.message)
        gate.set()
        blk.join(timeout=10)
        time.sleep(0.3)
        self.assertEqual(ran, [])

    def test_l3_client_socket_timeout(self):
        old = k.Client.TIMEOUT_S
        k.Client.TIMEOUT_S = 0.5
        try:
            gate, blk = self._block(3)
            c = k.Client(name="impatient")
            e = _err(c.call, "set", channel="2", v=100)
            self.assertIn("may or may not have been applied", e.message)
            c.close()
            gate.set()
            blk.join(timeout=10)
        finally:
            k.Client.TIMEOUT_S = old

    def test_l4_reload_keeps_poll_override(self):
        self.daemon.poll_override = "max"
        self.c.call("reload")
        self.assertEqual(self.daemon.cfg["poll_hz"], "max")

    def test_l5_set_while_on_refused_for_an_unguarded_channel(self):
        self.c.call("set", channel="2", v=0)
        self.c.call("set", channel="1", v=200, i=20)
        self.set_guard(guard({"1": {"max_cv": 300, "max_ma": 50}}))
        self.c.call("out", on=True)
        e = self.err("set", channel="2", v=400)
        self.assertEqual(e.code, "E_GUARD")
        self.assertIn("no limit", e.message)
        self.assertEqual(self.fake.v[2], 0)

    def test_l6_recall_zero_sets_voltage_and_current(self):
        self.fake.mem[3] = (3100, 5100, 3100, 5100)
        self.set_guard(guard({"all": {"max_cv": 500, "max_ma": 50}}))
        e = self.err("preset", n=3)
        self.assertEqual(e.code, "E_GUARD")
        self.assertEqual((self.fake.v[1], self.fake.i[1], self.fake.v[2], self.fake.i[2]), (0, 10, 0, 10))
        self.assertIn("CH1 0.00 V 0.010 A, CH2 0.00 V 0.010 A", e.message)

    def test_l9_expiry_emits_one_event_and_a_warning_when_on(self):
        self.c.call("set", channel="12", v=300, i=20)
        self.set_guard(guard({"all": {"max_cv": 500, "max_ma": 50}}))
        self.c.call("out", on=True)
        self.daemon.guard["expires_utc"] = (dt.datetime.now(UTC) - dt.timedelta(seconds=1)).isoformat()
        self.assertTrue(self.wait_for(lambda: any("expired" in e["message"] for e in self.events("guard"))))
        self.assertTrue(any("output is ON" in e["message"] for e in self.events("warning")))
        time.sleep(0.3)
        self.assertEqual(sum("expired" in e["message"] for e in self.events("guard")), 1)

    def test_l11_second_daemon_for_the_same_supply_refused(self):
        old = os.environ.get("KORAD_SOCKET")
        os.environ["KORAD_SOCKET"] = str(Path(self.tmp.name) / "second.sock")
        try:
            d2 = k.Daemon(dict(k.DEFAULT_CONFIG, **self.CFG), fake=True)
            e = _err(d2.serve)
            self.assertEqual(e.code, "E_DAEMON")
            self.assertIn(f"pid {os.getpid()}", e.message)
            self.assertFalse((Path(self.tmp.name) / "second.sock").exists())
        finally:
            if old is None:
                os.environ.pop("KORAD_SOCKET", None)
            else:
                os.environ["KORAD_SOCKET"] = old

    def test_l11_safe_stop_logs_no_false_panel_event(self):
        self.c.call("set", channel="12", v=300, i=20)
        self.daemon.safe_stop("test")
        time.sleep(0.3)
        self.assertFalse(self.events("panel"))

    def test_l13_partial_set_labels_every_write(self):
        real_write = self.fake.write

        def drop_vset1(data):
            if data.startswith(b"VSET1:"):
                return len(data)
            return real_write(data)
        self.fake.write = drop_vset1
        e = self.err("set", channel="12", v=500, i=40)
        self.assertIn("ISET1=0.040 A: verified", e.message)
        self.assertIn("ISET2=0.040 A: verified", e.message)
        self.assertIn("VSET1=5.00 V: sent, not verified", e.message)
        self.assertIn("VSET2=5.00 V: not sent", e.message)
        self.assertIn("Read-back now", e.message)

    def test_l32_bad_guard_file_reload_keeps_previous_guard(self):
        g = guard({"all": {"max_cv": 500}})
        self.set_guard(g)
        gf = Path(self.tmp.name) / "state" / "guard.json"
        gf.write_text(json.dumps(dict(g, limits={"all": {"max_cv": "5"}})))
        e = self.err("reload")
        self.assertEqual(e.code, "E_USAGE")
        self.assertIn("previous", e.message)
        self.assertEqual(self.daemon.guard["limits"]["all"]["max_cv"], 500)
        self.assertEqual(self.c.call("state")["device"], "present")
        e = _err(k.Daemon, dict(k.DEFAULT_CONFIG), fake=True)
        self.assertEqual(e.code, "E_USAGE")
        self.assertIn(str(gf), e.message)
        gf.write_text(json.dumps(g))


class LiveMessages(unittest.TestCase):
    def test_l21_stale_socket_message(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            path = Path(d) / "k.sock"
            s = socket.socket(socket.AF_UNIX)
            s.bind(str(path))
            s.close()                              # the file stays, nobody listens
            e = _err(k.Client, path)
            self.assertIn("stale socket", e.message)
            self.assertIn("daemon start", e.message)

    def test_l21_closed_connection_message(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            path = Path(d) / "k.sock"
            srv = socket.socket(socket.AF_UNIX)
            srv.bind(str(path))
            srv.listen(1)

            def accept_and_close():
                conn, _ = srv.accept()
                conn.recv(4096)
                conn.close()
            th = threading.Thread(target=accept_and_close, daemon=True)
            th.start()
            c = k.Client(path)
            e = _err(c.call, "ping")
            self.assertIn("may or may not have been applied", e.message)
            self.assertIn("korad status", e.message)
            c.close()
            srv.close()


class LiveCli(DaemonCase):
    run_cli = Cli.run_cli

    def one_json(self, r):
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        return json.loads(lines[0])

    def test_l20_json_after_the_command(self):
        r = self.run_cli("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.one_json(r)["ok"])

    def test_l20_json_alone_and_help(self):
        r = self.run_cli("--json")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.one_json(r)["error"]["code"], "E_USAGE")
        r = self.run_cli("--json", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage", self.one_json(r)["data"]["help"])
        r = self.run_cli("set", "--help", "--json")
        self.assertIn("--voltage", self.one_json(r)["data"]["help"])

    def test_l20_usage_errors_are_json(self):
        for args in (["set", "2", "-v", "5", "-v", "3"], ["preset", "٥"], ["set", "１", "-v", "1"],
                     ["guard", "set", "-v", "3", "--for", "99999999h"], ["set", "2", "-v", "3300MV"],
                     ["log", "-f", "/nonexistent/x.csv", "--duration", "1s"],
                     ["log", "-f", "/dev/stdout", "--duration", "1s"], ["monitor"],
                     ["daemon", "start", "--fake", "--poll", "0.5"], ["out"]):
            with self.subTest(args=args):
                r = self.run_cli("--json", *args)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertEqual(self.one_json(r)["error"]["code"], "E_USAGE")

    def test_l20_repeated_option_message(self):
        r = self.run_cli("set", "2", "-v", "5", "-v", "3")
        self.assertEqual(r.returncode, 2)
        self.assertIn("twice", r.stderr)

    def test_l20_internal_error_is_json_not_traceback(self):
        orig = k.cmd_status

        def boom(a, out):
            raise ValueError("synthetic")
        k.cmd_status = boom
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = k.main(["--json", "status"])
        finally:
            k.cmd_status = orig
        self.assertEqual(rc, 1)
        env = json.loads(buf.getvalue().strip())
        self.assertEqual(env["error"]["code"], "E_INTERNAL")
        self.assertIn("synthetic", env["error"]["message"])

    def test_l12_status_exits_5_while_the_device_is_absent(self):
        def fail_open():
            raise OSError("gone")
        self.daemon._open = fail_open
        self.daemon._next_open = 0
        self.daemon._drop("test detach")
        r = self.run_cli("--json", "--no-wait", "status")    # the immediate answer: stale values
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        env = self.one_json(r)
        self.assertEqual(env["error"]["code"], "E_DEVICE")
        self.assertIn("STALE", env["error"]["message"])
        self.assertTrue(env["error"]["detail"]["stale"])
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 5)
        self.assertIn("since", r.stderr)

    def test_minor_daemon_stop_returns_after_the_lock_is_free(self):
        r = self.run_cli("daemon", "stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        fd = os.open(self.daemon.lock_path(), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # raises if still held
        finally:
            os.close(fd)



# ---------------------------------------------------------------- attended live regressions

class AttendedDaemon(DaemonCase):
    def _absent(self):
        def fail_open():
            raise OSError("gone")
        self.daemon._open = fail_open
        self.daemon._next_open = 0
        self.daemon._drop("test detach")

    def _back(self):
        """Reconnect to the SAME fake supply (its state survives the 'detach')."""
        del self.daemon._open                      # the class method again
        real = k.FakeSerial
        k.FakeSerial = lambda: self.fake
        try:
            self.daemon._next_open = 0
            self.assertTrue(self.wait_for(lambda: self.daemon.dev is not None, 5))
        finally:
            k.FakeSerial = real

    def _jump(self, seconds):
        real_wall, real_now = k.wall_time, k.now_local
        k.wall_time = lambda: real_wall() + seconds
        k.now_local = lambda: real_now() + dt.timedelta(seconds=seconds)
        return real_wall, real_now

    def test_l2_1_forward_clock_jump_is_reported_and_names_the_expired_guard(self):
        self.set_guard(guard({"all": {"max_cv": 500}}, hours=0.5))
        time.sleep(0.2)
        saved = self._jump(4 * 3600)
        try:
            self.assertTrue(self.wait_for(lambda: self.events("clock"), 3))
            time.sleep(0.3)
            clock = self.events("clock")
            self.assertEqual(len(clock), 1, clock)
            self.assertIn("jumped +4h00m", clock[0]["message"])
            self.assertIn("this jump expired the guard", clock[0]["message"])
            self.assertRegex(clock[0]["message"], r"from .*\(\+\d\d:\d\d\) to .*\(")
            self.assertTrue(any("wall clock jumped" in e["message"] for e in self.events("warning")))
            self.assertTrue(any("guard expired" in e["message"] for e in self.events("guard")))
        finally:
            k.wall_time, k.now_local = saved

    def test_l2_1_backward_clock_jump_is_reported_guard_kept(self):
        self.set_guard(guard({"all": {"max_cv": 500}}, hours=0.5))
        time.sleep(0.2)
        saved = self._jump(-120)
        try:
            self.assertTrue(self.wait_for(lambda: self.events("clock"), 3))
            msg = self.events("clock")[0]["message"]
            self.assertIn("jumped -2m00s", msg)
            self.assertNotIn("expired", msg)
            self.assertTrue(self.c.call("state")["guard_active"])
        finally:
            k.wall_time, k.now_local = saved

    def test_l2_1_small_drift_is_not_a_jump(self):
        saved = self._jump(30)
        try:
            time.sleep(0.5)
            self.assertEqual(self.events("clock"), [])
        finally:
            k.wall_time, k.now_local = saved

    def test_l2_2_pending_off_applied_after_reconnect(self):
        self.c.call("set", channel="2", v=300, i=20)
        self.c.call("out", on=True)
        self._absent()
        e = self.err("out", on=False)
        self.assertEqual(e.code, "E_DEVICE")
        self.assertIn("applies this OFF as soon as the device is back", e.message)
        self.assertTrue(self.fake.out)                 # the 'detached' supply is still ON
        self._back()
        self.assertTrue(self.wait_for(lambda: any("pending OFF applied after reconnect" in ev["message"]
                                                  for ev in self.events("out")), 3))
        self.assertFalse(self.fake.out)

    def test_l2_2_pending_off_superseded_by_a_newer_on(self):
        self.c.call("set", channel="2", v=300, i=20)
        self.c.call("out", on=True)
        self._absent()
        self.err("out", on=False)
        self.err("out", on=True)                       # newer intent, also fails while absent
        self._back()
        self.assertTrue(self.wait_for(lambda: any("pending OFF discarded" in ev["message"]
                                                  for ev in self.events("out")), 3))
        self.assertTrue(self.fake.out)

    def test_l2_3_startup_reports_output_on_and_turns_it_off_under_the_guard(self):
        self.set_guard(guard({"all": {"max_cv": 500}}))
        self.fake.v[1], self.fake.v[2], self.fake.out = 0, 900, True
        rep = self.daemon.submit(self.daemon._startup_check)
        self.assertTrue(rep["output_on"])
        self.assertTrue(rep["turned_off"])
        self.assertFalse(self.fake.out)
        text = k.start_report_text(rep)
        self.assertIn("output was ON at start (status 0xc3): CH1 set 0.00 V", text)
        self.assertIn("the startup rule turned the output OFF: CH2 9.00 V", text)
        self.assertEqual(self.c.call("ping")["start_report"]["turned_off"], True)

    def test_l2_3_startup_reports_output_on_left_on_without_a_guard(self):
        self.fake.v[1], self.fake.v[2], self.fake.out = 0, 300, True
        rep = self.daemon.submit(self.daemon._startup_check)
        self.assertTrue(rep["output_on"] and not rep["turned_off"])
        self.assertTrue(self.fake.out)
        self.assertIn("(left ON)", k.start_report_text(rep))
        self.assertTrue(any("output was ON at start" in e["message"] for e in self.events("startup")))

    def test_l2_3_start_report_text_off_and_unknown(self):
        self.assertIn("output at start: OFF", k.start_report_text(
            {"output_on": False, "mode": "independent", "setpoints": sp(), "violations": [],
             "turned_off": False}))
        self.assertIn("unknown", k.start_report_text(None))

    def test_l2_4_verified_off_is_reported_ok_even_if_a_later_step_fails(self):
        self.c.call("set", channel="2", v=300, i=20)
        self.c.call("out", on=True)
        real = self.daemon._refresh

        def boom(*a, **kw):
            self.daemon._refresh = real
            raise OSError("link lost right after the read-back")
        self.daemon._refresh = boom
        data = self.c.call("out", on=False)
        self.assertTrue(data["verified"])
        self.assertFalse(self.fake.out)
        self.assertTrue(any("verified by read-back" in w for w in self.c.last_warnings),
                        self.c.last_warnings)
        self.assertFalse(any("may still be ON" in w for w in self.c.last_warnings))
        self.assertIsNone(self.daemon._pending_off)


class AttendedMeasuredLive(DaemonCase):
    CFG = {"poll_hz": 50, "setpoint_every_s": 100}   # setpoints are (almost) never re-read

    def test_l2_5_panel_knob_caught_by_measured_vout_before_setpoint_poll(self):
        self.set_guard(guard({"all": {"max_cv": 500}}))
        self.c.call("set", channel="12", v=0, i=20)
        self.c.call("set", channel="2", v=300)
        self.c.call("out", on=True)
        t0 = time.monotonic()
        self.fake.v[2] = 510                           # front-panel knob, 3.00 -> 5.10 V
        self.assertTrue(self.wait_for(lambda: not self.fake.out, 1))
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(self.c.call("state")["setpoints"]["v2"], 300)   # no setpoint poll ran
        off_events = lambda: [e["message"] for e in self.events("guard") if "turned OFF" in e["message"]]
        self.assertTrue(self.wait_for(off_events, 2))
        msg = off_events()
        self.assertTrue(msg and "CH2 measured 5.10 V is above the guard's 5.00 V" in msg[0], msg)

    def test_l2_5_within_margin_is_not_a_violation(self):
        self.set_guard(guard({"all": {"max_cv": 500}}))
        self.c.call("set", channel="12", v=0, i=20)
        self.c.call("set", channel="2", v=500)
        self.c.call("out", on=True)
        self.fake.v[2] = 502                           # 5.02 V: inside the 0.02 V margin
        time.sleep(0.3)
        self.assertTrue(self.fake.out)

    def test_l2_5_measured_units(self):
        g = guard({"all": {"max_cv": 3100}}, mode="series")
        g["limits"]["all"]["max_cv"] = 1000
        m = {"vout1": 5.5, "iout1": 0, "vout2": 5.5, "iout2": 0}
        self.assertEqual(k.measured_violations(g, "series", m),
                         ["series output measured 11.00 V is above the guard's 10.00 V"])
        self.assertEqual(k.measured_violations(g, "parallel", m), [])
        self.assertEqual(k.measured_violations(None, "independent", m), [])


class AttendedCli(DaemonCase):
    run_cli = Cli.run_cli

    def test_l2_3_daemon_start_prints_the_output_state(self):
        tmp = tempfile.mkdtemp(prefix="k", dir="/tmp")
        env = dict(os.environ, KORAD_HOME=tmp)
        try:
            r = subprocess.run([sys.executable, str(HERE / "korad.py"), "--json", "daemon", "start",
                                "--fake"], capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            data = json.loads(r.stdout.strip().splitlines()[-1])["data"]
            self.assertFalse(data["start_report"]["output_on"])
            r = subprocess.run([sys.executable, str(HERE / "korad.py"), "daemon", "stop"],
                               capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            subprocess.run([sys.executable, str(HERE / "korad.py"), "daemon", "stop"],
                           capture_output=True, env=env, timeout=30)
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ---- output bits, measured cross-check, OFF re-check, reconnect freshness (live)

class OutputBits(unittest.TestCase):
    def test_f4_1_either_bit_means_output_on(self):
        # 0x83: mains power-up with the output ON (bit 6 clear, bit 7 set), measured
        st = k.decode_status(0x83)
        self.assertTrue(st["output"])
        self.assertFalse(st["bit6"])
        self.assertTrue(st["bit7"])
        # 0x43: panel-ON state (bit 6 set, bit 7 clear)
        self.assertTrue(k.decode_status(0x43)["output"])
        self.assertTrue(k.decode_status(0xc3)["output"])
        self.assertFalse(k.decode_status(0x03)["output"])
        self.assertFalse(k.decode_status(0x33)["output"])

    def test_f4_1_fake_single_bit_states(self):
        for bits, raw in ((0x80, "0x83"), (0x40, "0x43"), (0xC0, "0xc3")):
            f = k.FakeSerial()
            d = k.Korad(f)
            d.set_v(1, 0)
            d.set_v(2, 100)
            f.out, f.out_bits = True, bits
            st = d.status()
            self.assertEqual(st["raw"], raw)
            self.assertTrue(st["output"], raw)

    def test_f4_1_out1_sets_both_bits_in_the_fake(self):
        f = k.FakeSerial()
        d = k.Korad(f)
        f.out_bits = 0x80
        self.assertTrue(d.set_output(True)["bit6"])

    def test_f4_3_off_verification_needs_both_bits_clear(self):
        f = k.FakeSerial()
        d = k.Korad(f)
        f.out, f.out_bits = True, 0x80
        real = f.write

        def no_out0(data):
            if data == b"OUT0":
                return len(data)        # the supply ignores the OFF
            return real(data)
        f.write = no_out0
        with self.assertRaises(k.KoradError) as cm:
            d.set_output(False)
        self.assertEqual(cm.exception.code, "E_VERIFY")
        self.assertRegex(cm.exception.message, r"status 0x8[0-3]")

    def test_f4_6_start_report_text_names_raw_status_and_measure(self):
        sp = {"v1": 0, "i1": 0, "v2": 100, "i2": 100}
        rep = {"output_on": True, "by_measure": False, "raw": "0x83", "mode": "independent",
               "setpoints": sp, "violations": [], "turned_off": False,
               "measured": {"vout1": 0, "vout2": 1.0}}
        self.assertIn("output was ON at start (status 0x83)", k.start_report_text(rep))
        rep.update(raw="0x03", by_measure=True)
        text = k.start_report_text(rep)
        self.assertIn("status 0x03, which says OFF, but VOUT reads 0.00 / 1.00 V", text)


class OutputBitsDaemon(DaemonCase):
    def _power_up_on(self, v2, bits):
        """The supply as after a mains power cycle: output ON with a partial status byte."""
        self.fake.v[1], self.fake.i[1] = 0, 0
        self.fake.v[2], self.fake.i[2] = v2, 100
        self.fake.out, self.fake.out_bits = True, bits

    def test_f4_1_bit7_only_output_is_on_and_guarded(self):
        self.set_guard(guard({"all": {"max_cv": 500, "max_ma": 200}}))
        self._power_up_on(900, 0x80)                    # 9 V, STATUS 0x83
        self.assertTrue(self.wait_for(lambda: not self.fake.out, 2))
        self.assertTrue(self.wait_for(
            lambda: any("turned OFF" in e["message"] for e in self.events("guard")), 2))

    def test_f4_1_bit7_only_within_guard_shows_on(self):
        self._power_up_on(100, 0x80)
        self.assertTrue(self.wait_for(
            lambda: (self.c.call("state")["status"] or {}).get("raw") == "0x83", 2))
        st = self.c.call("state")["status"]
        self.assertTrue(st["output"])
        self.assertTrue(st["bit7"])
        self.assertFalse(st["bit6"])

    def test_f4_2_measured_voltage_counts_as_on_when_status_lies(self):
        self._power_up_on(100, 0)                       # status reads OFF, 1 V at the terminals
        warn = lambda: [e for e in self.events("warning") if "ON by measured voltage" in e["message"]]
        self.assertTrue(self.wait_for(warn, 2))
        self.assertIn("raw 0x03", warn()[0]["message"])
        for _ in range(5):                              # stays ON by measure, poll after poll
            st = self.c.call("state")["status"]
            self.assertTrue(st["output"], st)
            self.assertTrue(st.get("output_by_measure"), st)
            time.sleep(0.15)
        self.assertEqual(len(warn()), 1)                # one warning per episode

    def test_f4_2_measured_on_breaking_the_guard_is_turned_off(self):
        self.set_guard(guard({"all": {"max_cv": 500}}))
        self._power_up_on(900, 0)
        self.assertTrue(self.wait_for(lambda: not self.fake.out, 2))
        self.assertTrue(self.wait_for(
            lambda: any("turned OFF" in e["message"] for e in self.events("guard")), 2))

    def test_f4_2_no_false_alarm_during_decay_then_off_rechecked(self):
        self.c.call("set", channel="2", v=300, i=20)
        self.c.call("out", on=True)
        self.c.call("out", on=False)
        t0 = time.monotonic()
        self.fake.out, self.fake.out_bits = True, 0     # decay: voltage present, status OFF
        time.sleep(1.0)
        self.assertEqual([e for e in self.events("warning") if "ON by measured" in e["message"]], [])
        still = lambda: [e for e in self.events("warning") if "still reads" in e["message"]]
        self.assertTrue(self.wait_for(still, 2))       # the 1.5 s re-check after our OFF
        self.assertGreaterEqual(time.monotonic() - t0, k.OFF_DECAY_S - 0.1)
        self.assertIn("sending OUT0 again", still()[0]["message"])
        self.assertTrue(self.wait_for(lambda: not self.fake.out, 1))

    def test_f4_3_two_failed_offs_warn(self):
        self.c.call("set", channel="2", v=300, i=20)
        self.c.call("out", on=True)
        real = self.fake.write

        def sticky(data):
            r = real(data)
            if data == b"OUT0":
                self.fake.out, self.fake.out_bits = True, 0   # the terminals stay live
            return r
        self.fake.write = sticky
        self.c.call("out", on=False)
        two = lambda: [e for e in self.events("warning") if "after two OFF commands" in e["message"]]
        self.assertTrue(self.wait_for(two, 5))
        self.fake.write = real
        self.fake.out = False

    def test_f4_5_status_waits_for_a_sample_after_reconnect(self):
        with self.daemon.cache_cv:
            self.daemon.cache["opened_at"] = time.time() + 0.6
        out = k.Out(True, "status")
        t0 = time.monotonic()
        s = k._state(self.c, out)
        self.assertGreaterEqual(s["t"], s["opened_at"])
        self.assertGreater(time.monotonic() - t0, 0.4)
        self.assertEqual(out.warnings, [])

    def test_f4_6_reconnect_event_names_output_on_and_raw(self):
        real_open = k.Daemon._open

        def fail_open(self_):
            raise OSError("gone")
        self.daemon._open = fail_open.__get__(self.daemon)
        self.daemon._next_open = 0
        self.daemon._drop("test detach")
        self._power_up_on(100, 0x80)
        del self.daemon._open
        real = k.FakeSerial
        k.FakeSerial = lambda: self.fake
        try:
            self.daemon._next_open = 0
            self.assertTrue(self.wait_for(lambda: self.daemon.dev is not None, 5))
        finally:
            k.FakeSerial = real
        msgs = lambda: [e["message"] for e in self.events("warning") if "output is ON at open" in e["message"]]
        self.assertTrue(self.wait_for(msgs, 2))
        self.assertIn("status 0x83", msgs()[0])
        self.assertIn("M1", msgs()[0])


# ---- F4 items 7-10: hung port, stale sample, several matching nodes, forced stop (Windows sleep)

class HungPort(DaemonCase):
    def setUp(self):
        super().setUp()
        self.daemon.WATCHDOG_S = 1.0

    def test_f4_7_watchdog_closes_a_hung_port_and_reopens(self):
        hung = self.fake
        hung.hang = True
        ev = lambda: [e["message"] for e in self.events("device") if "device hung: port closed" in e["message"]]
        self.assertTrue(self.wait_for(ev, 4), self.events("device"))
        self.assertTrue(self.wait_for(lambda: self.daemon.dev is not None and self.daemon.dev.port is not hung, 5))
        self.assertTrue(self.wait_for(lambda: self.c.call("state")["device"] == "present", 3))

    def test_f4_7_out_off_on_a_hung_port_fails_fast_and_is_kept_pending(self):
        self.c.call("set", channel="2", v=300, i=20)
        self.c.call("out", on=True)
        self.fake.hang = True
        t0 = time.monotonic()
        e = self.err("out", on=False)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(e.code, "E_DEVICE")
        self.assertIn("applies this OFF", e.message)

    def test_f4_7_serial_open_has_a_write_timeout(self):
        import inspect
        self.assertIn("write_timeout", inspect.getsource(k.Daemon._open))


class StaleSample(unittest.TestCase):
    def _s(self, age, hz=5):
        return {"device": "present", "t": time.time() - age, "poll_hz": hz, "seq": 9,
                "status": None, "measured": None, "setpoints": None, "guard_text": "guard: NONE",
                "setpoints_t": None}

    def test_f4_8_old_sample_while_present_is_an_error(self):
        with self.assertRaises(k.KoradError) as cm:
            k._check_fresh(self._s(97))
        self.assertEqual(cm.exception.code, "E_DEVICE")
        self.assertIn("no fresh sample for 97 s", cm.exception.message)
        self.assertEqual(k.EXIT[cm.exception.code], 5)

    def test_f4_8_fresh_sample_passes(self):
        k._check_fresh(self._s(0.5))
        k._check_fresh(self._s(2.5, hz="max"))
        k._check_fresh(dict(self._s(97), device="absent"))   # absent is reported elsewhere


class SeveralNodes(unittest.TestCase):
    def setUp(self):
        self._real = k.find_ports

    def tearDown(self):
        k.find_ports = self._real

    def test_f4_9_stale_node_is_skipped(self):
        k.find_ports = lambda serial="", **kw: ["/dev/ttyACM0", "/dev/ttyACM1"]
        name, skipped = k.find_port("", probe=lambda n: n == "/dev/ttyACM1")
        self.assertEqual((name, skipped), ("/dev/ttyACM1", ["/dev/ttyACM0"]))

    def test_f4_9_single_node_is_not_probed(self):
        k.find_ports = lambda serial="", **kw: ["/dev/ttyACM3"]
        self.assertEqual(k.find_port("", probe=lambda n: self.fail("probed")), ("/dev/ttyACM3", []))

    def test_f4_9_no_node_answers(self):
        k.find_ports = lambda serial="", **kw: ["/dev/ttyACM0", "/dev/ttyACM1"]
        with self.assertRaises(k.KoradError) as cm:
            k.find_port("", probe=lambda n: False)
        self.assertIn("none answers", cm.exception.message)

    def test_f4_9_probe_reads_the_identity(self):
        f = k.FakeSerial()
        self.assertTrue(k.probe_korad("/dev/fake", opener=lambda n: f, timeout=0.3))
        dead = k.FakeSerial()
        dead.write = lambda data: len(data)             # never answers
        self.assertFalse(k.probe_korad("/dev/fake", opener=lambda n: dead, timeout=0.3))


STUCK_DAEMON = r"""
import json, os, signal, socketserver, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN)          # a daemon stuck in its safe stop
class H(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            op = json.loads(line)["op"]
            data = {"pid": os.getpid(), "lock": None} if op == "ping" else {"stopping": True}
            self.wfile.write((json.dumps({"ok": True, "data": data, "warnings": []}) + "\n").encode())
            self.wfile.flush()
class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
S(sys.argv[1], H).serve_forever()
"""


class ForcedStop(unittest.TestCase):
    def test_f4_10_stop_terminates_a_stuck_daemon_and_exits_5(self):
        tmp = tempfile.mkdtemp(prefix="k", dir="/tmp")
        env_old = {x: os.environ.get(x) for x in ("KORAD_HOME", "KORAD_SOCKET")}
        os.environ["KORAD_HOME"] = tmp
        os.environ.pop("KORAD_SOCKET", None)
        sock = k.socket_path()
        sock.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([sys.executable, "-c", STUCK_DAEMON, str(sock)])
        wait_s = k.STOP_WAIT_S
        try:
            deadline = time.monotonic() + 5
            while not sock.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            k.STOP_WAIT_S = 1.0
            a = argparse_ns(dcmd="stop")
            out = k.Out(True, "daemon")
            t0 = time.monotonic()
            with self.assertRaises(k.KoradError) as cm:
                k.cmd_daemon(a, out)
            self.assertEqual(cm.exception.code, "E_DEVICE")
            self.assertEqual(k.EXIT["E_DEVICE"], 5)
            self.assertIn("SIGKILL", cm.exception.message)
            self.assertIn("UNKNOWN", cm.exception.message)
            self.assertLess(time.monotonic() - t0, 12)
            self.assertIsNotNone(proc.wait(timeout=5))
            self.assertFalse(sock.exists())
        finally:
            k.STOP_WAIT_S = wait_s
            if proc.poll() is None:
                proc.kill()
            for key, val in env_old.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


def argparse_ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- power-up / M1 flag

class PresetSaveWarningUnit(unittest.TestCase):
    def test_d2_slot1_nonzero_warns_power_up_voltage_and_procedure(self):
        w = k.preset_save_warning(1, {"v1": 0, "i1": 10, "v2": 180, "i2": 20})
        self.assertIn("M1 is the power-up slot", w)
        self.assertIn("front panel's last On/Off state as of power-off", w)
        self.assertIn("OUT0/OUT1 over USB do not change it", w)
        self.assertIn("CH2 1.80 V", w)
        self.assertNotIn("CH1", w)
        self.assertIn("can come up ON", w)
        self.assertIn("korad preset-save 1", w)
        self.assertIn("M2-M5", w)

    def test_d2_slot1_zero_confirms_safe(self):
        w = k.preset_save_warning(1, {"v1": 0, "i1": 10, "v2": 0, "i2": 10})
        self.assertIn("M1 is the power-up slot", w)
        self.assertIn("puts no voltage on the outputs", w)
        self.assertNotIn("can come up ON", w)

    def test_d2_other_slots_no_warning(self):
        for n in (2, 3, 4, 5):
            self.assertIsNone(k.preset_save_warning(n, {"v1": 500, "i1": 10, "v2": 500, "i2": 10}))

    def test_d2_safe_procedure_names_the_commands(self):
        self.assertIn("korad set 12 -v 0 -i 0'", k.SAFE_M1_PROCEDURE)
        self.assertIn("korad preset-save 1", k.SAFE_M1_PROCEDURE)
        self.assertNotIn("press On/Off", k.SAFE_M1_PROCEDURE)


class PresetSaveWarningDaemon(Cli):
    def test_d2_recall_and_other_slots_have_no_save_warning(self):
        self.c.call("preset", n=2)
        self.assertFalse(any("power-up slot" in w for w in self.c.last_warnings))
        self.c.call("preset", n=3, save=True)
        self.assertFalse(any("power-up slot" in w for w in self.c.last_warnings))


# ---------------------------------------------------------------- M1 protection

class M1Protection(Cli):
    """M1 is the verified power-up slot at 0 V / 0 A; the daemon never saves non-zero into it."""

    def zero(self):
        self.c.call("set", channel="12", v=0, i=0)

    def test_p_nonzero_save_refused_exit_3(self):
        self.c.call("set", channel="2", v=180, i=20)
        mem_before = self.fake.mem[1]
        with self.assertRaises(k.KoradError) as cm:
            self.c.call("preset", n=1, save=True)
        self.assertEqual(cm.exception.code, "E_GUARD")
        self.assertIn("M1 is the power-up slot and must stay at 0 V / 0 A", cm.exception.message)
        self.assertIn("M2-M5", cm.exception.message)
        self.assertEqual(self.fake.mem[1], mem_before)
        r = self.run_cli("--json", "preset-save", "1")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        env = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(env["error"]["code"], "E_GUARD")
        audit = (self.daemon.audit_file).read_text()
        self.assertIn("preset-save 1 refused", audit)

    def test_p_nonzero_current_only_is_refused_too(self):
        self.c.call("set", channel="12", v=0, i=0)
        self.c.call("set", channel="1", i=5)
        with self.assertRaises(k.KoradError) as cm:
            self.c.call("preset", n=1, save=True)
        self.assertEqual(cm.exception.code, "E_GUARD")

    def test_p_m1_already_zero_is_not_overwritten(self):
        self.zero()
        self.fake.mem[1] = (0, 0, 0, 0)
        writes = []
        orig = self.fake.write

        def spy(data):
            writes.append(data.decode("latin1"))
            return orig(data)
        self.fake.write = spy
        r = self.c.call("preset", n=1, save=True)
        self.fake.write = orig
        self.assertIsNone(r["saved"])
        self.assertIn("M1 already holds 0/0; not overwritten", r["message"])
        self.assertNotIn("SAV1", writes)
        self.assertEqual(self.fake.mem[1], (0, 0, 0, 0))
        self.assertEqual(r["setpoints"], {"v1": 0, "i1": 0, "v2": 0, "i2": 0})
        self.assertEqual((self.fake.v[1], self.fake.i[1], self.fake.v[2], self.fake.i[2]), (0, 0, 0, 0))
        self.assertIn("M1 already holds 0/0", self.daemon.audit_file.read_text())

    def test_p_m1_nonzero_is_restored_to_zero(self):
        self.zero()
        self.fake.mem[1] = (330, 123, 3100, 5100)
        r = self.c.call("preset", n=1, save=True)
        self.assertEqual(r["saved"], 1)
        self.assertIn("M1 restored to 0/0 (it held CH1 3.30 V 0.123 A, CH2 31.00 V 5.100 A)", r["message"])
        self.assertEqual(self.fake.mem[1], (0, 0, 0, 0))
        self.assertEqual((self.fake.v[1], self.fake.i[1], self.fake.v[2], self.fake.i[2]), (0, 0, 0, 0))
        self.assertIn("M1 restored to 0/0", self.daemon.audit_file.read_text())

    def test_p_refused_while_output_on(self):
        self.zero()
        self.fake.mem[1] = (0, 0, 100, 10)
        self.c.call("out", on=True)
        with self.assertRaises(k.KoradError) as cm:
            self.c.call("preset", n=1, save=True)
        self.assertEqual(cm.exception.code, "E_USAGE")
        self.assertIn("turn the output off first", cm.exception.message)
        self.assertEqual(self.fake.mem[1], (0, 0, 100, 10))
        self.assertTrue(self.fake.out)

    def test_p_recall_m1_still_allowed(self):
        self.fake.mem[1] = (0, 0, 0, 0)
        r = self.c.call("preset", n=1)
        self.assertEqual(r["recalled"], 1)

    def test_p_cli_text_prints_the_message(self):
        self.zero()
        self.fake.mem[1] = (0, 0, 0, 0)
        r = self.run_cli("ps", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("M1 already holds 0/0; not overwritten", r.stdout)


# ---------------------------------------------------------------- identity + usbipd re-attach

import types  # noqa: E402

SER_A, SER_B = "ABC123", "XYZ789"   # placeholders, not real device serials


def _state_json(*devs):
    out = []
    for busid, uid, ser, client, guid in devs:
        vid, pid = uid.upper().split(":")
        out.append({"BusId": busid, "ClientIPAddress": client, "Description": "USB Serial Device",
                    "InstanceId": f"USB\\VID_{vid}&PID_{pid}\\{ser}", "IsForced": False,
                    "PersistedGuid": guid, "StubInstanceId": None})
    return json.dumps({"Devices": out})


class FakeRun:
    """Stands in for subprocess.run on usbipd.exe: no real usbipd calls."""

    def __init__(self, state=None, list_out=None, attach_rc=0):
        self.state, self.list_out, self.attach_rc, self.calls = state, list_out, attach_rc, []

    def __call__(self, argv, timeout):
        self.calls.append(argv[1:])
        if argv[1] == "state":
            if self.state is None:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="unknown command")
            return types.SimpleNamespace(returncode=0, stdout=self.state, stderr="")
        if argv[1] == "list":
            return types.SimpleNamespace(returncode=0, stdout=self.list_out or "", stderr="")
        if argv[1] == "attach":
            return types.SimpleNamespace(returncode=self.attach_rc, stdout="",
                                         stderr="" if self.attach_rc == 0 else "usbipd: error: boom")
        raise AssertionError(argv)


class IdentityUnits(unittest.TestCase):
    def test_b_usb_id_validation(self):
        self.assertEqual(k.check_usb_id("0416:5011"), "0416:5011")
        self.assertEqual(k.check_usb_id("0416:ABCD"), "0416:abcd")
        for bad in ("0416-5011", "416:5011", "0416:50111", "zz16:5011", "０４１６:5011", "", 5011):
            with self.assertRaises(k.KoradError) as cm:
                k.check_usb_id(bad)
            self.assertEqual(cm.exception.code, "E_USAGE")

    def test_b_usb_serial_validation(self):
        self.assertEqual(k.check_usb_serial(""), "")
        self.assertEqual(k.check_usb_serial(SER_A), SER_A)
        for bad in ("AB C", "Ä1", "x" * 65, None):
            with self.assertRaises(k.KoradError):
                k.check_usb_serial(bad)

    def test_b_config_identity_and_reattach_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text(f'usb_id = "0416:5011"\nusb_serial = "{SER_A}"\nusbipd_reattach = "off"\n')
            cfg = k.load_config(p)
            self.assertEqual((cfg["usb_id"], cfg["usb_serial"], cfg["usbipd_reattach"]),
                             ("0416:5011", SER_A, "off"))
            p.write_text('usb_id = "0416_5011"\n')
            with self.assertRaises(k.KoradError):
                k.load_config(p)
            p.write_text('usbipd_reattach = "yes"\n')
            with self.assertRaises(k.KoradError):
                k.load_config(p)

    def test_b_reattach_default_follows_wsl_detection(self):
        real_wsl, real_exe = k.running_in_wsl, k.usbipd_exe
        try:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "none.toml"
                k.running_in_wsl, k.usbipd_exe = (lambda: True), (lambda c="": "/x/usbipd.exe")
                self.assertEqual(k.load_config(p)["usbipd_reattach"], "wsl")
                k.running_in_wsl = lambda: False
                self.assertEqual(k.load_config(p)["usbipd_reattach"], "off")
                k.running_in_wsl, k.usbipd_exe = (lambda: True), (lambda c="": "")
                self.assertEqual(k.load_config(p)["usbipd_reattach"], "off")
        finally:
            k.running_in_wsl, k.usbipd_exe = real_wsl, real_exe

    def test_b_no_env_override(self):
        self.assertNotIn("KORAD_USB_SERIAL", (HERE / "korad.py").read_text())


class SerialAwarePorts(unittest.TestCase):
    def scan(self, nodes):
        return lambda usb_id: nodes

    def test_b_different_serials_refused_without_usb_serial(self):
        nodes = [("/dev/ttyACM0", SER_A), ("/dev/ttyACM1", SER_B)]
        with self.assertRaises(k.KoradError) as cm:
            k.find_ports("", scan=self.scan(nodes))
        self.assertEqual(cm.exception.code, "E_DEVICE")
        self.assertIn(SER_A, cm.exception.message)
        self.assertIn(SER_B, cm.exception.message)
        self.assertIn("usb_serial", cm.exception.message)

    def test_b_usb_serial_selects(self):
        nodes = [("/dev/ttyACM0", SER_A), ("/dev/ttyACM1", SER_B)]
        self.assertEqual(k.find_ports(SER_B, scan=self.scan(nodes)), ["/dev/ttyACM1"])

    def test_b_same_serial_several_nodes_kept_for_probing(self):
        nodes = [("/dev/ttyACM0", SER_A), ("/dev/ttyACM1", SER_A)]
        self.assertEqual(k.find_ports("", scan=self.scan(nodes)), ["/dev/ttyACM0", "/dev/ttyACM1"])

    def test_b_scan_acm_reads_a_sysfs_tree_by_usb_id(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for i, (vid, pid, ser) in enumerate((("0416", "5011", SER_A), ("0403", "6001", SER_B))):
                usb = root / "usb" / f"1-{i}"
                (usb / f"1-{i}:1.0").mkdir(parents=True)
                (usb / "idVendor").write_text(vid + "\n")
                (usb / "idProduct").write_text(pid + "\n")
                (usb / "serial").write_text(ser + "\n")
                tty = root / "tty" / f"ttyACM{i}"
                tty.mkdir(parents=True)
                (tty / "device").symlink_to(usb / f"1-{i}:1.0")
            self.assertEqual(k.scan_acm("0416:5011", root / "tty"), [("/dev/ttyACM0", SER_A)])
            self.assertEqual(k.scan_acm("0403:6001", root / "tty"), [("/dev/ttyACM1", SER_B)])


class UsbipdParsing(unittest.TestCase):
    def test_a_state_json(self):
        run = FakeRun(_state_json(("1-2", "0416:5011", SER_A, None, "g1"),
                                  ("3-1", "0416:5011", SER_B, "127.0.0.1", "g2"),
                                  ("4-4", "0416:5011", "5&abc&0&3", None, None),
                                  (None, "0416:5011", "OLD", None, "g3")))
        devs = k.usbipd_devices("usbipd.exe", run)
        self.assertEqual([(d["busid"], d["serial"], d["state"]) for d in devs],
                         [("1-2", SER_A, "shared"), ("3-1", SER_B, "attached"), ("4-4", "", "not_shared")])

    def test_a_list_fallback(self):
        out = ("Connected:\nBUSID  VID:PID    DEVICE                              STATE\n"
               "1-2    0416:5011  USB Serial Device (COM3)            Shared\n"
               "1-6    046d:c539  USB Input Device                    Not shared\n"
               "3-3    0925:3881  fx2lafw                             Attached\n\n"
               "Persisted:\nGUID                                  DEVICE\n")
        devs = k.usbipd_devices("usbipd.exe", FakeRun(None, out))
        self.assertEqual([(d["busid"], d["usb_id"], d["serial"], d["state"]) for d in devs],
                         [("1-2", "0416:5011", None, "shared"), ("1-6", "046d:c539", None, "not_shared"),
                          ("3-3", "0925:3881", None, "attached")])

    def test_a_pick(self):
        devs = [{"busid": "1-2", "usb_id": "0416:5011", "serial": SER_A, "client": None, "state": "shared"},
                {"busid": "2-7", "usb_id": "0416:5011", "serial": SER_B, "client": None, "state": "shared"}]
        self.assertIsNone(k.pick_usbipd_device(devs, "0416:5011", "")[0])
        self.assertEqual(k.pick_usbipd_device(devs, "0416:5011", SER_B)[0]["busid"], "2-7")
        self.assertIsNone(k.pick_usbipd_device(devs, "0416:5011", "NOPE")[0])
        self.assertIsNone(k.pick_usbipd_device([], "0416:5011", "")[0])
        self.assertEqual(k.pick_usbipd_device(devs[:1], "0416:5011", "")[0]["busid"], "1-2")


class Reattach(DaemonCase):
    def setUp(self):
        super().setUp()
        self.daemon.cfg = dict(self.daemon.cfg, usbipd_path=sys.executable, usbipd_reattach="wsl",
                               usb_id="0416:5011", usb_serial="")
        self.events = []
        real = self.daemon.event
        self.daemon.event = lambda kind, src, msg, *a, **kw: (self.events.append(msg), real(kind, src, msg, *a, **kw))

    def use(self, run):
        self.daemon._usbipd_run = run
        return run

    def test_a_shared_device_is_attached_by_its_current_busid(self):
        run = self.use(FakeRun(_state_json(("5-3", "0416:5011", SER_A, None, "g"))))
        msg = self.daemon._reattach_once()
        self.assertIn(["attach", "--wsl", "--busid", "5-3"], run.calls)
        self.assertEqual(msg, "usbipd re-attach: busid 5-3 attached")
        self.assertIn(msg, self.events)

    def test_a_attached_elsewhere_is_not_taken_and_logged_once(self):
        run = self.use(FakeRun(_state_json(("1-2", "0416:5011", SER_A, "192.0.2.9", "g"))))
        m1 = self.daemon._reattach_once()
        self.daemon._reattach_once()
        self.assertFalse(any(c[0] == "attach" for c in run.calls))
        self.assertIn("not taking it", m1)
        self.assertEqual(sum("not taking it" in e for e in self.events), 1)

    def test_a_not_shared_asks_for_a_bind_and_never_binds(self):
        run = self.use(FakeRun(_state_json(("1-2", "0416:5011", SER_A, None, None))))
        msg = self.daemon._reattach_once()
        self.assertIn("usbipd bind --busid 1-2", msg)
        self.assertIn("never binds", msg)
        self.assertEqual([c[0] for c in run.calls], ["state"])

    def test_a_missing_device(self):
        self.use(FakeRun(_state_json(("1-6", "046d:c539", "X1", None, None))))
        self.assertIn("no 0416:5011 device on the Windows host bus", self.daemon._reattach_once())

    def test_a_two_devices_different_serials(self):
        state = _state_json(("1-2", "0416:5011", SER_A, None, "g"), ("2-9", "0416:5011", SER_B, None, "g"))
        run = self.use(FakeRun(state))
        msg = self.daemon._reattach_once()
        self.assertIn("set usb_serial", msg)
        self.assertFalse(any(c[0] == "attach" for c in run.calls))
        self.daemon.cfg["usb_serial"] = SER_B
        run = self.use(FakeRun(state))
        self.daemon._reattach_once()
        self.assertIn(["attach", "--wsl", "--busid", "2-9"], run.calls)

    def test_a_attach_failure_is_reported(self):
        self.use(FakeRun(_state_json(("1-2", "0416:5011", SER_A, None, "g")), attach_rc=1))
        msg = self.daemon._reattach_once()
        self.assertIn("attach failed (rc 1)", msg)
        self.assertIn("boom", msg)

    def test_a_gating_fake_port_and_rate_limit(self):
        started = []
        real_thread = k.threading.Thread

        class T:
            def __init__(self, target, daemon):
                started.append(target)

            def start(self):
                pass
        k.threading.Thread = T
        try:
            d = self.daemon
            d.absent_since = time.time() - 10
            d._maybe_reattach()                       # fake daemon: never
            self.assertEqual(started, [])
            d.fake = False
            d.port_name = "/dev/ttyACM9"
            d._maybe_reattach()                       # fixed port: never
            self.assertEqual(started, [])
            d.port_name = None
            d.absent_since = time.time() - 1
            d._maybe_reattach()                       # absent only 1 s: not yet
            self.assertEqual(started, [])
            d.absent_since = time.time() - 10
            d._maybe_reattach()
            self.assertEqual(len(started), 1)
            d._reattach_busy.release()
            d._maybe_reattach()                       # within 10 s of the last try
            self.assertEqual(len(started), 1)
            d.cfg["usbipd_reattach"] = "off"
            d._reattach_last = 0
            d._maybe_reattach()
            self.assertEqual(len(started), 1)
        finally:
            k.threading.Thread = real_thread
            self.daemon.fake = True


class IdentityCli(Cli):
    def test_b_bad_usb_id_exit_2(self):
        r = self.run_cli("--json", "daemon", "start", "--foreground", "--fake", "--usb-id", "zz:5011")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["error"]["code"], "E_USAGE")

    def test_b_override_reaches_the_daemon_and_survives_reload(self):
        d = k.Daemon(k.load_config(), fake=True, id_override={"usb_serial": SER_A, "usb_id": None})
        self.assertEqual(d.cfg["usb_serial"], SER_A)
        self.assertEqual(d.cfg["usb_id"], k.DEFAULT_USB_ID)
        d.event = lambda *a, **kw: None
        d.h_reload({}, "test")
        self.assertEqual(d.cfg["usb_serial"], SER_A)


# ---------------------------------------------------------------- start on first use, idle stop

class FirstUse(unittest.TestCase):
    """CLI commands start the daemon when none answers (config autostart, default on).
    The config sets fake = true, so every daemon started here runs on FakeSerial."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="kA2.", dir="/tmp")    # short: AF_UNIX path limit
        (Path(self.home) / "config").mkdir()
        self.write_cfg()
        self.env = dict(os.environ, KORAD_HOME=self.home)
        self.env.pop("KORAD_SOCKET", None)

    def write_cfg(self, extra=""):
        (Path(self.home) / "config" / "config.toml").write_text(
            "fake = true\npoll_hz = 20\nusbipd_reattach = \"off\"\n" + extra)

    def tearDown(self):
        self.cli("daemon", "stop")
        for line in subprocess.run(["pgrep", "-f", "korad.py daemon start --foreground"],
                                   capture_output=True, text=True).stdout.split():
            try:
                env = Path(f"/proc/{line}/environ").read_bytes()
            except OSError:
                continue
            if f"KORAD_HOME={self.home}".encode() in env:
                os.kill(int(line), 9)
        subprocess.run(["rm", "-rf", self.home])

    def cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / "korad.py"), *args], capture_output=True,
                              text=True, env=self.env, timeout=60)

    def started_lines(self, text):
        return [x for x in text.splitlines() if "daemon started on first use (pid" in x]

    def daemon_starts(self):
        log = Path(self.home) / "state" / "daemon.log"
        return len(re.findall(r"daemon \[daemon\] started, pid", log.read_text())) if log.exists() else 0

    def test_status_starts_the_daemon(self):
        r = self.cli("status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.started_lines(r.stderr)), 1, r.stderr)
        self.assertIn("output at start: OFF", r.stderr)
        r2 = self.cli("status")                         # now it runs: no second start
        self.assertEqual(r2.returncode, 0)
        self.assertEqual(self.started_lines(r2.stderr), [])
        self.assertEqual(self.daemon_starts(), 1)

    def test_set_starts_the_daemon_and_runs(self):
        r = self.cli("set", "2", "-v", "1", "-i", "20mA")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.started_lines(r.stderr)), 1)
        self.assertIn("CH2 set 1.00 V 0.020 A", r.stdout)

    def test_json_puts_the_note_in_warnings(self):
        r = self.cli("--json", "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        env = json.loads(lines[0])
        self.assertTrue(any("daemon started on first use" in w for w in env["warnings"]))

    def test_autostart_false_keeps_exit_5(self):
        self.write_cfg("autostart = false\n")
        r = self.cli("--json", "status")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout.strip())["error"]["code"], "E_DAEMON")
        self.assertEqual(self.daemon_starts(), 0)

    def test_daemon_status_does_not_start_it(self):
        r = self.cli("daemon", "status")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("daemon not running", r.stderr)
        self.assertFalse((Path(self.home) / "state" / "korad.sock").exists())
        self.assertEqual(self.daemon_starts(), 0)

    def test_concurrent_first_use_gives_one_daemon(self):
        procs = [subprocess.Popen([sys.executable, str(HERE / "korad.py"), "--json", "status"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
                 for _ in range(3)]
        outs = [p.communicate(timeout=60) for p in procs]
        for p, (o, e) in zip(procs, outs):
            self.assertEqual(p.returncode, 0, o + e)
        self.assertEqual(self.daemon_starts(), 1)
        pids = {json.loads(self.cli("--json", "daemon", "status").stdout)["data"]["daemon"]["pid"]}
        self.assertEqual(len(pids), 1)


class IdleStop(DaemonCase):
    CFG = dict(DaemonCase.CFG, idle_stop_after_s=0.4)

    def _absent(self):
        def fail_open():
            raise OSError("gone")
        self.daemon._open = fail_open
        self.daemon._next_open = 0
        self.daemon._drop("test detach")

    def test_absent_and_no_guard_stops(self):
        self._absent()
        self.assertTrue(self.wait_for(self.daemon.stopping.is_set, 5))
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())
        msgs = [e["message"] for e in self.daemon.events]
        self.assertTrue(any(m.startswith("idle stop: device absent for") for m in msgs), msgs)
        self.assertFalse(any("SAFE STOP" in m for m in msgs))
        self.assertFalse(k.socket_path().exists())
        self.assertTrue(k._wait_lock_free(self.daemon.lock_path(), 1))

    def test_active_guard_keeps_it_running(self):
        self.set_guard(guard({"all": {"max_cv": 500}}))
        self._absent()
        time.sleep(1.2)
        self.assertFalse(self.daemon.stopping.is_set())

    def test_expired_guard_does_not_keep_it(self):
        g = guard({"all": {"max_cv": 500}})
        g["expires_utc"] = (dt.datetime.now(UTC) - dt.timedelta(minutes=5)).isoformat()
        self.daemon.guard = g
        self._absent()
        self.assertTrue(self.wait_for(self.daemon.stopping.is_set, 5))

    def test_present_device_keeps_it_running(self):
        time.sleep(1.2)
        self.assertFalse(self.daemon.stopping.is_set())
        self.c.call("ping")

    def test_zero_means_never(self):
        self.daemon.cfg = dict(self.daemon.cfg, idle_stop_after_s=0)
        self._absent()
        time.sleep(1.2)
        self.assertFalse(self.daemon.stopping.is_set())

    def test_reattach_success_restarts_the_clock(self):
        self.daemon.cfg = dict(self.daemon.cfg, idle_stop_after_s=60, usbipd_path=sys.executable,
                               usbipd_reattach="wsl", usb_id="0416:5011", usb_serial="")
        self.daemon.dev = None
        self.daemon.absent_since = time.time() - 100
        self.assertTrue(self.daemon._idle_stop_due())
        self.daemon._usbipd_run = FakeRun(_state_json(("1-2", "0416:5011", SER_A, None, "g")))
        self.daemon._reattach_once()
        self.assertLess(time.time() - self.daemon.absent_since, 2)
        self.assertFalse(self.daemon._idle_stop_due())


class ConfigKeys(unittest.TestCase):
    def test_new_keys_validated(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            for bad in ("autostart = 1", "fake = \"yes\"", "idle_stop_after_s = -1",
                        "idle_stop_after_s = true", "idle_stop_after_s = nan"):
                p.write_text(bad + "\n")
                with self.assertRaises(k.KoradError, msg=bad):
                    k.load_config(p)
            p.write_text("autostart = false\nidle_stop_after_s = 0\nfake = true\n")
            cfg = k.load_config(p)
            self.assertEqual((cfg["autostart"], cfg["idle_stop_after_s"], cfg["fake"]), (False, 0, True))
        self.assertEqual((k.DEFAULT_CONFIG["autostart"], k.DEFAULT_CONFIG["idle_stop_after_s"]), (True, 300))


# ---------------------------------------------------------------- waiting for the supply

class SlowRun(FakeRun):
    """FakeRun with a delay per usbipd call, so every phase is visible to a polling client."""

    def __init__(self, *a, delay=0.4, **kw):
        super().__init__(*a, **kw)
        self.delay, self.attached_at = delay, None

    def __call__(self, argv, timeout):
        time.sleep(self.delay)
        r = super().__call__(argv, timeout)
        if argv[1] == "attach" and r.returncode == 0:
            self.attached_at = time.monotonic()
        return r


class WaitForDevice(Cli):
    CFG = dict(Cli.CFG, idle_stop_after_s=0)

    def detach(self, run):
        """Make the daemon a non-fake WSL daemon whose supply is gone; `run` mocks usbipd.exe."""
        d = self.daemon
        d.cfg = dict(d.cfg, usbipd_path=sys.executable, usbipd_reattach="wsl", usb_id="0416:5011",
                     usb_serial="", port="")
        d._usbipd_run = run
        orig = d._open

        def fake_open():
            if run.attached_at is None or time.monotonic() - run.attached_at < 0.4:
                raise OSError("no 0416:5011 serial device found")
            d._phase("opening", "opening /dev/ttyACM0 ...")
            time.sleep(0.4)
            d.fake = True
            try:
                orig()
            finally:
                d.fake = False
        d._open = fake_open
        d.fake = False
        d._next_open = 0
        d._drop("test detach")

    def tearDown(self):
        self.daemon.fake = True
        super().tearDown()

    def out(self):
        return k.Out(True, "test")

    def test_w_progress_lines_in_order_absent_to_opened(self):
        run = SlowRun(_state_json(("1-2", "0416:5011", SER_A, None, "g")))
        self.detach(run)
        o = self.out()
        s = k.wait_for_device(self.c, o, limit=10, poll=0.05)
        self.assertEqual(s["device"], "present")
        text = "\n".join(o.warnings)
        want = ["waiting for the supply: device absent", "looking for 0416:5011",
                "busid 1-2 is shared, attaching to WSL", "busid 1-2 attached; waiting for the serial port",
                "opening /dev/ttyACM0", "supply reached"]
        pos = [text.find(w) for w in want]
        self.assertTrue(all(p >= 0 for p in pos), text)
        self.assertEqual(pos, sorted(pos), text)
        self.assertTrue(all(re.match(r"^\[\s*\d+\.\d s\] ", w) for w in o.warnings), o.warnings)

    def _final(self, state, expect):
        self.detach(SlowRun(state, delay=0.05))
        t0 = time.monotonic()
        with self.assertRaises(k.KoradError) as cm:
            k.wait_for_device(self.c, self.out(), limit=15, poll=0.05)
        self.assertLess(time.monotonic() - t0, 5)
        e = cm.exception
        self.assertEqual(e.code, "E_DEVICE")
        self.assertEqual(k.EXIT[e.code], 5)
        self.assertIn(expect, e.message)
        self.assertIn("waiting will not fix this", e.message)
        self.assertTrue(e.detail["reattach"]["final"])
        return e

    def test_w_final_not_on_bus(self):
        e = self._final(_state_json(("1-6", "046d:c539", "X1", None, None)), "no 0416:5011 device")
        self.assertIn("powered on", e.message)

    def test_w_final_not_shared(self):
        self._final(_state_json(("1-2", "0416:5011", SER_A, None, None)), "usbipd bind --busid 1-2")

    def test_w_final_attached_elsewhere(self):
        self._final(_state_json(("1-2", "0416:5011", SER_A, "192.0.2.9", "g")), "not taking it")

    def test_w_final_fixed_port(self):
        self.detach(SlowRun(_state_json(("1-2", "0416:5011", SER_A, None, "g"))))
        self.daemon.port_name = "/dev/ttyACM9"
        try:
            with self.assertRaises(k.KoradError) as cm:
                k.wait_for_device(self.c, self.out(), limit=15, poll=0.05)
            self.assertIn("fixed port", cm.exception.message)
        finally:
            self.daemon.port_name = None

    def test_w_timeout(self):
        self.detach(SlowRun(_state_json(("1-2", "0416:5011", SER_A, None, "g")), attach_rc=1, delay=0.05))
        t0 = time.monotonic()
        with self.assertRaises(k.KoradError) as cm:
            k.wait_for_device(self.c, self.out(), limit=1.5, poll=0.05)
        self.assertGreaterEqual(time.monotonic() - t0, 1.4)
        self.assertIn("not reached within 1.5 s", cm.exception.message)
        self.assertIn("attach failed", cm.exception.message)
        self.assertFalse(cm.exception.detail["reattach"]["final"])

    def test_w_present_device_no_wait_no_lines(self):
        o = self.out()
        self.assertIsNone(k.wait_for_device(self.c, o, limit=10))
        self.assertEqual(o.warnings, [])

    def test_w_json_one_stdout_line_progress_in_warnings(self):
        self.detach(SlowRun(_state_json(("1-2", "0416:5011", SER_A, None, "g"))))
        r = self.run_cli("--json", "set", "2", "-v", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, r.stdout)
        env = json.loads(lines[0])
        self.assertTrue(env["ok"])
        self.assertTrue(any("supply reached" in w for w in env["warnings"]), env["warnings"])
        self.assertNotIn("supply reached", r.stderr)

    def test_w_human_progress_on_stderr(self):
        self.detach(SlowRun(_state_json(("1-2", "0416:5011", SER_A, None, "g"))))
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("waiting for the supply", r.stderr)
        self.assertIn("supply reached", r.stderr)
        self.assertNotIn("supply reached", r.stdout)

    def test_w_no_wait_answers_at_once(self):
        self.detach(SlowRun(_state_json(("1-2", "0416:5011", SER_A, None, "g")), delay=2))
        t0 = time.monotonic()
        r = self.run_cli("status", "--no-wait")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertLess(time.monotonic() - t0, 4)
        self.assertNotIn("waiting for the supply", r.stderr)
        self.assertIn("STALE", r.stderr)

    def test_w_out_off_does_not_wait(self):
        self.detach(SlowRun(_state_json(("1-2", "0416:5011", SER_A, None, "g")), delay=2))
        t0 = time.monotonic()
        r = self.run_cli("out", "off")
        self.assertLess(time.monotonic() - t0, 4)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertNotIn("waiting for the supply", r.stderr)
        self.assertIn("as soon as the device is back", r.stderr)

    def test_w_config_key(self):
        cfg = k.load_config(Path(self.tmp.name) / "nope.toml")
        self.assertEqual(cfg["wait_for_device_s"], 15)
        p = Path(self.tmp.name) / "c.toml"
        for bad in ("-1", "true", "\"5\""):
            p.write_text(f"wait_for_device_s = {bad}\n")
            with self.assertRaises(k.KoradError):
                k.load_config(p)
        p.write_text("wait_for_device_s = 0\n")
        self.assertEqual(k.load_config(p)["wait_for_device_s"], 0)
