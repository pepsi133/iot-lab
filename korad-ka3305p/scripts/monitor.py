"""korad monitor: a curses view of the daemon's cache with keyboard control.

Started by `korad monitor [--csv FILE]`. A client of the daemon like every
other command: all writes go through the daemon and its guard. Quitting leaves
the output as it is; the daemon owns the safe stop. Keys: see ../DESIGN.md.

The key handling is pure (`handle_key`, `parse_command`) so tests can drive it
without a terminal.
"""
from __future__ import annotations

import csv
import curses
import datetime as dt
import os
import threading
import time
from dataclasses import dataclass, field as dc_field

import korad
from korad import KoradError, MAX_CV, MAX_MA, fmt_i, fmt_v

STEPS = {"v": (1, 10, 100), "i": (1, 10, 100)}     # centivolts / milliamps
STEP_NAMES = {"v": ("0.01 V", "0.1 V", "1.00 V"), "i": ("0.001 A", "0.010 A", "0.100 A")}
CEIL = {"v": MAX_CV, "i": MAX_MA}
UP = {"d": 1, "k": 1, "D": 10, "K": 10, "f": -1, "j": -1, "F": -10, "J": -10}
CHANNEL = {"a": 1, "h": 1, "s": 2, "l": 2}
OFF_KEYS = (" ", "x", "p")
ON_KEYS = ("t", "y")
DOUBLE_PRESS_S = 1.0     # the second press must come within this time of the first ...
MIN_GAP_S = 0.15         # ... and at least this long after the previous press (key repeat is faster)
ON_SETTLE_S = 0.12       # ON fires only if no further press of the key follows within this time
MAX_KEY_STEP = {"v": 500, "i": 500}   # one key press changes at most 5.00 V / 0.500 A
HELP = ("d/k up  f/j down  D/K F/J x10 | a/h CH1  s/l CH2 | e/i V<->I | c/n step | "
        "tt/yy ON  space/x/p OFF | : command | q quit")
CSV_COLUMNS = ["time_iso", "t_rel_s", "vset1", "iset1", "vout1", "iout1", "vset2", "iset2",
               "vout2", "iout2", "output", "mode", "ch1_cv", "ch2_cv", "ovp", "ocp", "event"]


@dataclass
class UIState:
    channel: int = 1
    field: str = "v"               # "v" or "i"
    step: int = 1                  # index into STEPS
    mode: str = "independent"
    setpoints: dict | None = None  # {"v1","i1","v2","i2"} from the daemon
    pending: dict = dc_field(default_factory=dict)   # {(ch, field): target} not yet confirmed
    on_armed_at: float | None = None
    on_armed_key: str | None = None
    on_last_press: float | None = None
    on_fire_at: float | None = None    # a confirmed double press waits here for ON_SETTLE_S
    message: str = ""


def edit_channel(st: UIState) -> int:
    """In series/parallel, CH2 drives both channels, so edits go to CH2."""
    return 2 if st.mode in ("series", "parallel") else st.channel


def current_value(st: UIState, ch: int, fld: str):
    if (ch, fld) in st.pending:
        return st.pending[(ch, fld)]
    if st.setpoints is None:
        return None
    return st.setpoints[f"{fld}{ch}"]


def handle_key(st: UIState, key: str, now: float) -> list[tuple]:
    """Apply one key to the UI state. Returns actions for the caller to run:
    ("set", ch, field, value), ("out", bool), ("cmdline",), ("quit",)."""
    if st.on_armed_at is not None and key not in ON_KEYS:
        _disarm(st)
    if key in OFF_KEYS:
        _disarm(st)
        st.message = "output OFF sent"
        return [("out", False)]
    if key in ON_KEYS:
        if st.on_armed_key == key and st.on_last_press is not None and now - st.on_last_press < MIN_GAP_S:
            # Key auto-repeat: a held key never turns the output ON.
            st.on_last_press = now
            st.on_fire_at = None
            st.message = f"{key} is held: release it, then press {key} twice"
            return []
        if st.on_fire_at is not None and st.on_armed_key == key:
            # A third press right after the double press: that is a held key too.
            st.on_fire_at = None
            st.on_armed_at = None
            st.on_last_press = now
            st.message = f"{key} is held: release it, then press {key} twice"
            return []
        if st.on_armed_at is not None and st.on_armed_key == key and now - st.on_armed_at <= DOUBLE_PRESS_S:
            st.on_last_press = now
            st.on_fire_at = now + ON_SETTLE_S
            st.message = "output ON ..."
            return []
        st.on_armed_at, st.on_armed_key, st.on_last_press, st.on_fire_at = now, key, now, None
        st.message = f"press {key} again within 1 s to turn the output ON"
        return []
    if key == "q":
        return [("quit",)]
    if key == ":":
        return [("cmdline",)]
    if key in CHANNEL:
        st.channel = CHANNEL[key]
        if st.mode in ("series", "parallel") and st.channel == 1:
            st.message = f"{st.mode} mode: CH1 follows CH2, edits go to CH2"
        return []
    if key in ("e", "i"):
        st.field = "i" if st.field == "v" else "v"
        return []
    if key in ("c", "n"):
        st.step = (st.step + 1) % 3
        st.message = f"step {STEP_NAMES[st.field][st.step]} / {STEP_NAMES['i' if st.field == 'v' else 'v'][st.step]}"
        return []
    if key in UP:
        ch = edit_channel(st)
        cur = current_value(st, ch, st.field)
        if cur is None:
            st.message = "no setpoints yet: wait for the first poll"
            return []
        delta = UP[key] * STEPS[st.field][st.step]
        delta = max(-MAX_KEY_STEP[st.field], min(MAX_KEY_STEP[st.field], delta))
        new = max(0, min(CEIL[st.field], cur + delta))
        if new == cur:
            return []
        st.pending[(ch, st.field)] = new
        if ch != st.channel:
            st.message = f"{st.mode} mode: CH1 follows CH2, edit applied to CH2"
        return [("set", ch, st.field, new)]
    return []


def _disarm(st: UIState):
    st.on_armed_at = st.on_armed_key = st.on_last_press = st.on_fire_at = None


def tick(st: UIState, now: float) -> list[tuple]:
    """Call often. Fires a confirmed double press once no key repeat followed it."""
    if st.on_fire_at is not None and now >= st.on_fire_at:
        _disarm(st)
        st.message = "output ON sent"
        return [("out", True)]
    return []


def parse_command(text: str) -> tuple:
    """`:` command line. Returns one action or raises KoradError."""
    parts = text.strip().split()
    if not parts:
        return ("noop",)
    cmd = parts[0].lower()
    if cmd in ("q", "quit"):
        return ("quit",)
    if len(cmd) == 2 and cmd[0] in "vi" and cmd[1] in "12" and len(parts) == 2:
        val = parts[1]
        if val.endswith("m"):                  # shorthand: 50m = 50 mA, 500m = 500 mV (M is mega)
            val += "A" if cmd[0] == "i" else "V"
        return ("set", int(cmd[1]), cmd[0], korad.parse_value(val, cmd[0]))
    if cmd == "out" and len(parts) == 2 and parts[1] in ("on", "off"):
        return ("out", parts[1] == "on")
    if cmd == "mode" and len(parts) == 2 and parts[1] in korad.MODE_CMD:
        return ("mode", parts[1])
    if cmd == "preset" and len(parts) == 2 and parts[1] in "12345" and len(parts[1]) == 1:
        return ("preset", int(parts[1]))
    raise KoradError("E_USAGE", f"unknown command {text.strip()!r}: use v1 3.3, i2 50m, "
                     f"out on|off, mode independent|series|parallel, preset 1-5, q")


# ---------------------------------------------------------------- daemon I/O

class Sender(threading.Thread):
    """Sends the latest target per channel at most once per 100 ms."""

    def __init__(self, ui: "Monitor"):
        super().__init__(daemon=True)
        self.ui = ui
        self.lock = threading.Lock()
        self.targets: dict = {}          # (ch, field) -> value
        self.jobs: list = []             # other actions, in order
        self.wake = threading.Event()
        self.client = None

    def push_target(self, ch, fld, value):
        with self.lock:
            self.targets[(ch, fld)] = value
        self.wake.set()

    def push_job(self, action):
        with self.lock:
            if action[0] == "out" and action[1] is False:
                self.jobs.insert(0, action)     # OFF first
            else:
                self.jobs.append(action)
        self.wake.set()

    def _connect(self):
        c, note = korad.connect_or_start(name="monitor")
        if note:
            self.ui.say(note)
        return c

    def _call(self, op, **kw):
        try:
            if self.client is None:
                self.client = self._connect()
            return self.client.call(op, **kw)
        except KoradError as e:
            if e.code == "E_DAEMON":
                self.client = None
            raise
        except OSError as e:
            self.client = None
            raise KoradError("E_DAEMON", f"lost the daemon: {e}")

    def run(self):
        while not self.ui.stop.is_set():
            try:
                self._loop()
            except Exception as e:          # never let the sender die silently
                self.client = None
                self.ui.say(f"sender error: {e}")
                time.sleep(0.5)

    def _loop(self):
        last_set = 0.0
        while not self.ui.stop.is_set():
            self.wake.wait(0.1)
            self.wake.clear()
            with self.lock:
                jobs, self.jobs = self.jobs, []
            for job in jobs:
                self._run_job(job)
            wait = 0.1 - (time.monotonic() - last_set)
            if wait > 0:
                time.sleep(wait)
            with self.lock:
                targets, self.targets = self.targets, {}
            if not targets:
                continue
            last_set = time.monotonic()
            by_ch: dict = {}
            for (ch, fld), val in targets.items():
                by_ch.setdefault(ch, {})[fld] = val
            for ch, vals in by_ch.items():
                try:
                    data = self._call("set", channel=str(ch), **vals)
                    self.ui.on_set_done(ch, vals, data, None)
                except KoradError as e:
                    self.ui.on_set_done(ch, vals, None, e)

    def _run_job(self, job):
        try:
            if job[0] == "out":
                self._call("out", on=job[1])
                self.ui.say(f"output {'ON' if job[1] else 'OFF'}")
            elif job[0] == "mode":
                self._call("mode", mode=job[1])
                self.ui.say(f"mode {job[1]}; the output is OFF")
            elif job[0] == "preset":
                self._call("preset", n=job[1])
                self.ui.say(f"preset M{job[1]} recalled; the output is OFF")
            w = self.client.last_warnings if self.client else []  # noqa: client set by _call
            if w:
                self.ui.say("WARNING " + w[0])
        except KoradError as e:
            self.ui.say(f"{e.code}: {e.message}")


class Monitor:
    def __init__(self, csv_path: str | None):
        self.ui = UIState()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.state: dict | None = None
        self.client = None
        self.events_after = 0
        self.events: list = []
        self.last_seq = 0
        self.t0: float | None = None       # time of the first CSV sample
        self.csv_path = csv_path
        self.csv_file = self.csv_writer = None
        self.cmdline: str | None = None
        self.daemon_error = ""
        self.sender = Sender(self)

    def say(self, msg):
        with self.lock:
            self.ui.message = msg

    def on_set_done(self, ch, vals, data, err):
        with self.lock:
            for fld, val in vals.items():
                if self.ui.pending.get((ch, fld)) == val:
                    del self.ui.pending[(ch, fld)]
            if err:
                for fld in vals:
                    self.ui.pending.pop((ch, fld), None)
                self.ui.message = f"{err.code}: {err.message}"
            else:
                self.ui.setpoints = data["after"]
                self.ui.message = f"CH{ch} set: " + ", ".join(
                    fmt_v(data["after"][f"v{ch}"]) if f == "v" else fmt_i(data["after"][f"i{ch}"]) for f in vals)

    # -- polling
    def _connect(self):
        """Connect; with autostart on (config), start the daemon again if it went away."""
        c, note = korad.connect_or_start(name="monitor")
        if note:
            self.say(note)
        return c

    def poll(self):
        try:
            if self.client is None:
                self.client = self._connect()
            s = self.client.call("state", events_after=self.events_after)
        except (KoradError, OSError) as e:
            self.client = None
            self.daemon_error = getattr(e, "message", str(e))
            return
        self.daemon_error = ""
        if s.get("last_event_seq", 0) < self.events_after:     # the daemon restarted
            self.events_after = 0
            s = self.client.call("state", events_after=0)
        new_events = s.get("events", [])
        if new_events:
            self.events_after = new_events[-1]["seq"]
            self.events = (self.events + new_events)[-50:]
        with self.lock:
            self.state = s
            if s.get("setpoints"):
                self.ui.setpoints = s["setpoints"]
            if s.get("status"):
                self.ui.mode = s["status"]["mode"]
        if self.csv_writer and s.get("seq", 0) > self.last_seq and s.get("measured"):
            self.last_seq = s["seq"]
            self.write_csv(s, new_events)
        elif new_events and self.csv_writer:
            self._carry = getattr(self, "_carry", []) + new_events

    def write_csv(self, s, new_events):
        carry = getattr(self, "_carry", [])
        self._carry = []
        sp, m, stt = s.get("setpoints") or {}, s["measured"], s.get("status") or {}
        t = s["t"]
        if self.t0 is None:
            self.t0 = t
        ev = " | ".join(f"[{e['source']}] {e['message']}" for e in carry + new_events)
        self.csv_writer.writerow([
            dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds"),
            f"{t - self.t0:.3f}",
            f"{sp['v1'] / 100:.2f}" if sp else "", f"{sp['i1'] / 1000:.3f}" if sp else "",
            f"{m['vout1']:.2f}", f"{m['iout1']:.3f}",
            f"{sp['v2'] / 100:.2f}" if sp else "", f"{sp['i2'] / 1000:.3f}" if sp else "",
            f"{m['vout2']:.2f}", f"{m['iout2']:.3f}",
            int(bool(stt.get("output"))), stt.get("mode", ""), int(bool(stt.get("ch1_cv"))),
            int(bool(stt.get("ch2_cv"))), int(bool(stt.get("ovp"))), int(bool(stt.get("ocp"))), ev])
        self.csv_file.flush()

    # -- actions
    def run_actions(self, actions):
        for a in actions:
            if a[0] == "quit":
                return False
            if a[0] == "cmdline":
                self.cmdline = ""
            elif a[0] == "set":
                self.sender.push_target(a[1], a[2], a[3])
            elif a[0] in ("out", "mode", "preset"):
                self.sender.push_job(a)
        return True

    def command(self, text) -> bool:
        try:
            a = parse_command(text)
        except KoradError as e:
            self.say(e.message)
            return True
        if a[0] == "noop":
            return True
        if a[0] == "set":
            ch = a[1]
            if self.ui.mode in ("series", "parallel") and ch == 1:
                self.say(f"{self.ui.mode} mode: CH1 follows CH2, use v2/i2")
                return True
            with self.lock:
                self.ui.pending[(ch, a[2])] = a[3]
        return self.run_actions([a])

    # -- drawing
    def draw(self, scr):
        scr.erase()
        h, w = scr.getmaxyx()

        def put(y, x, text, attr=0):
            if 0 <= y < h and x < w - 1:
                try:
                    scr.addstr(y, x, str(text)[: max(0, w - 1 - x)], attr)
                except curses.error:
                    pass

        with self.lock:
            s, ui = self.state, self.ui
            msg = ui.message.splitlines()[0] if ui.message else ""
        y = 0
        put(y, 0, "korad monitor", curses.A_BOLD)
        if self.daemon_error:
            put(y, 15, f"DAEMON: {self.daemon_error}  (retrying)", curses.A_REVERSE)
            y += 2
        elif s:
            st = s.get("status") or {}
            out_on = st.get("output")
            put(y, 15, " OUTPUT ON " if out_on else " output off ",
                (curses.color_pair(1) | curses.A_BOLD | curses.A_REVERSE) if out_on else curses.A_DIM)
            age = f"{time.time() - s['t']:.1f} s" if s.get("t") else "-"
            put(y, 28, f"mode {st.get('mode', '?')}  OVP {'on' if st.get('ovp') else 'off'}  "
                       f"OCP {'on' if st.get('ocp') else 'off'}  poll {s.get('poll_hz')} Hz  "
                       f"sample age {age}  device {s.get('device')}")
            y += 2
            put(y, 0, f"{'':4}{'set V':>10}{'set I':>11}{'out V':>10}{'out I':>11}  mode   "
                      f"step {STEP_NAMES[ui.field][ui.step]}")
            y += 1
            sp = s.get("setpoints") or {}
            m = s.get("measured") or {}
            ech = edit_channel(ui)
            for ch in (1, 2):
                sel = ch == ech
                put(y, 0, f"CH{ch}", curses.A_BOLD if sel else 0)
                for fld, x in (("v", 4), ("i", 14)):
                    val = current_value(ui, ch, fld)
                    txt = "-" if val is None else (f"{val / 100:.2f} V" if fld == "v" else f"{val / 1000:.3f} A")
                    if (ch, fld) in ui.pending:
                        txt = "*" + txt
                    attr = curses.A_REVERSE if (sel and ui.field == fld) else 0
                    put(y, x + (10 if fld == "v" else 11) - len(txt), txt, attr)
                if m:
                    put(y, 25, f"{m[f'vout{ch}']:>8.2f} V{m[f'iout{ch}']:>9.3f} A")
                cv = st.get(f"ch{ch}_cv")
                put(y, 47, "CV" if cv else "CC")
                y += 1
            if ui.mode in ("series", "parallel"):
                put(y, 0, f"{ui.mode}: CH1 follows CH2", curses.A_DIM)
            y += 1
            for line in (s.get("guard_text") or "").splitlines():
                put(y, 0, line, curses.A_BOLD if not s.get("guard_active") else 0)
                y += 1
        else:
            put(2, 0, "waiting for the daemon...")
            y = 4
        y += 1
        put(y, 0, msg, curses.color_pair(2) | curses.A_BOLD if msg else 0)
        y += 2
        put(y, 0, "events:", curses.A_DIM)
        y += 1
        for e in self.events[-6:]:
            t = dt.datetime.fromtimestamp(e["t"]).strftime("%H:%M:%S")
            put(y, 0, f"{t} {e['kind']:<8} [{e['source']}] {e['message']}")
            y += 1
        if self.cmdline is not None:
            put(h - 2, 0, ":" + self.cmdline, curses.A_REVERSE)
        put(h - 1, 0, HELP, curses.A_DIM)
        scr.refresh()

    # -- main loop
    def loop(self, scr):
        curses.curs_set(0)
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
        except curses.error:
            pass
        scr.timeout(100)
        self.sender.start()
        self.poll()
        s = self.state
        if s is not None and not s.get("guard_active"):
            self.say("WARNING " + korad.no_guard_warning(s.get("guard")))
        last_poll = time.monotonic()
        while True:
            if time.monotonic() - last_poll >= 0.1:
                self.poll()
                last_poll = time.monotonic()
            with self.lock:
                due = tick(self.ui, time.monotonic())
            if due and not self.run_actions(due):
                return
            self.draw(scr)
            try:
                ch = scr.get_wch()
            except curses.error:
                continue
            if ch == curses.KEY_RESIZE:
                continue
            if isinstance(ch, int):
                if self.cmdline is not None and ch in (curses.KEY_BACKSPACE,):
                    self.cmdline = self.cmdline[:-1]
                continue
            if self.cmdline is not None:
                if ch in ("\n", "\r"):
                    text, self.cmdline = self.cmdline, None
                    if not self.command(text):
                        return
                elif ch == "\x1b":
                    self.cmdline = None
                elif ch in ("\x7f", "\b"):
                    self.cmdline = self.cmdline[:-1]
                elif ch.isprintable():
                    self.cmdline += ch
                continue
            with self.lock:
                actions = handle_key(self.ui, ch, time.monotonic())
            if not self.run_actions(actions):
                return

    def main(self):
        if self.csv_path:
            new = not (os.path.exists(self.csv_path) and os.path.getsize(self.csv_path) > 0)
            self.csv_file = open(self.csv_path, "a", newline="")
            self.csv_writer = csv.writer(self.csv_file)
            if new:
                self.csv_writer.writerow(CSV_COLUMNS)
        try:
            curses.wrapper(self.loop)
        finally:
            self.stop.set()
            if self.csv_file:
                self.csv_file.close()
        return 0


def run(csv_path: str | None = None) -> int:
    return Monitor(csv_path).main()


if __name__ == "__main__":
    import sys
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))
