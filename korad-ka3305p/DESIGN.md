# korad: design record

The design of `scripts/korad.py`. The measured protocol facts that it relies
on are in [README.md](README.md), "Protocol behavior".

## Architecture

- **One daemon owns the serial port.** It opens the port (exclusive) and
  listens on a Unix socket. Every other command is a client of the daemon.
- **Start on first use.** A command that needs the daemon starts it when none
  answers, and prints one line on stderr: `daemon started on first use (pid
  N): <start report>` (under `--json`, a warning). `korad daemon start` still
  exists for options such as `--poll max`. `daemon status` and `daemon stop`
  never start it. `autostart = false` in the config restores the old rule:
  exit 5 until someone runs `daemon start`. When several commands start it at
  the same moment, the per-supply lock lets one daemon win and all of them use
  it. The monitor starts it again when it goes away. So an agent or a person
  does not have to manage the daemon.
- **Idle stop.** The daemon stops itself when the device has been absent for
  `idle_stop_after_s` (default 300 s, 0 = never) **and** no guard is active.
  It logs `idle stop: ...` and exits without a safe stop, because there is no
  device to talk to. An active guard keeps it running, so it re-attaches the
  supply and enforces the guard after a power cycle. A present device keeps it
  running whether or not any client is connected. A successful USB/IP
  re-attach restarts the absent clock.
- **Waiting for the supply, with progress.** The daemon keeps a connection
  phase (`reattach` in `state` and `ping`): `idle`, `waiting`, `looking`,
  `attaching`, `attached`, `opening`, `failed`, `blocked`, with a message, the
  busid, the time it began, the seconds to the next re-attach try, and `final`.
  `final` marks a cause that waiting will not fix: the supply is not on the
  Windows bus, it is not shared, it is attached to another client, several
  supplies match without `usb_serial`, the port is busy, or re-attach is off
  (fixed port, not WSL). A command that needs the supply (status, set, out on,
  mode, preset, preset-save, ovp, ocp, log, monitor) waits up to
  `wait_for_device_s` (default 15 s, 0 = no wait) when the device is absent.
  It first sends `reconnect`, so the daemon looks at once instead of at its
  next timed try, then prints each phase change with the elapsed time on
  stderr (in `warnings` under `--json`, so stdout stays one line). It stops at
  once on a final phase newer than its `reconnect`, and after the time limit
  otherwise; both exit 5 with the cause and the next step. `--no-wait` (any
  position) answers at once. `out off` never waits: it tries at once, and a
  failed OFF stays pending. A command must say what it is doing and why it
  takes longer.
- The daemon runs one device thread. Requests go into a priority queue.
  **Output OFF has the highest priority**, so it waits at most for the one
  command in progress (about 50 ms).
- A single poller fills a cache: measured V/I and `STATUS?` every cycle, the
  setpoints every `setpoint_every_s`. `status`, `monitor` and `log` read the
  cache, so more clients add no serial traffic. `poll_hz = "max"` polls with
  no pause, for fast data collection.
- **Stop is always safe.** `daemon stop`, SIGTERM and Ctrl-C do `OUT0`, then
  CH1/CH2 to 0.00 V / 0.010 A, then independent mode, and read all of it back.
  A SIGKILL or a lost USB link cannot do this. The supply keeps its last
  state.
- On start, the daemon does not change the device, except for one case: if the
  output is ON and the live state breaks the guard, it turns the output OFF.
  `daemon start` prints the output state it found (`output at start: OFF`, or
  `output was ON at start: ...` and whether the startup rule turned it OFF),
  also as `start_report` in the `--json` data.
- **An OFF that cannot reach the device is kept as a pending OFF.** The daemon
  applies it (OUT0, read back) as soon as the device is back, and logs
  `pending OFF applied after reconnect`. A newer output request replaces it.
- **The `out off` answer follows the read-back.** When the read-back showed
  OFF, the answer is ok, even if a later step failed (that failure becomes a
  warning). "May still be ON" appears only when no read-back showed OFF.
- A busy queue cannot starve the poller: before a queued command, the daemon
  polls first when the last poll is older than 2 poll periods or 0.5 s.
- `daemon stop` exits 5 when the safe stop failed, for example with the
  device absent. The output can then still be ON. If the safe stop does not
  finish within 10 s (a hung device thread), `daemon stop` says so, exits 5,
  and terminates the daemon (SIGTERM, then SIGKILL after 5 s), so the lock and
  the socket are free again. The supply state is then unknown.
- **Output bits.** `STATUS?` bit 6 **or** bit 7 means the output is ON.
  `OUT1` sets both bits and `OUT0` clears both. But after a mains power cycle
  the supply came up with the output ON and read `0x83` (bit 6 clear, bit 7
  set), and a front-panel ON state read `0x43` (bit 6 set, bit 7
  clear). An OFF is verified only when both bits are clear. The status shows
  both bits as `bit6` and `bit7`.
- **Power-up and M1.** At power-up the supply always loads M1's setpoints
  and sets the output to the front panel's own On/Off state as it was at
  power-off. `OUT0`/`OUT1` over USB do not change that state, so the tool
  cannot control the output at power-up. The adopted design: M1 is a safe
  slot at 0.00 V / 0.000 A on both channels, and working presets live in
  M2–M5. Save M1 at 0/0 once on the front panel, with both outputs OFF (the
  tool cannot set the panel On/Off state), and it powers up OFF at 0/0. **The
  daemon never saves non-zero values into M1:** `preset-save 1` with any
  setpoint above 0 is refused with `E_GUARD`, with no override. With all
  setpoints at 0 and the output OFF, it recalls M1 to read it, restores the
  setpoints (write and read back), and saves only when M1 holds anything
  above 0/0, then reads M1 again to verify. With the output ON it is
  refused (`E_USAGE`), because the recall would turn the output off. Every
  outcome is an audit event. `preset 1` (recall) stays allowed. The open
  report names the safe-M1 procedure when it finds the output ON.
- **Measured cross-check.** When `STATUS?` says OFF but a measured VOUT is
  above 0.10 V for 2 samples in a row, and no OFF (ours, `TRACK`, `RCL`, or an
  OFF seen from the panel) came in the last 1.5 s, the daemon counts the
  output as ON (`output_by_measure: true`), logs one warning, and runs the
  live guard checks. The 1.5 s window covers the decay after OFF (up to about
  1.1 s, measured).
- **OFF re-check.** 1.5 s after an OFF that the daemon sent, a VOUT still above
  0.10 V gives a warning and one more `OUT0`. If VOUT is still up after that,
  a second warning says the output may be ON.
- **Hung port watchdog.** The port opens with a 0.5 s read timeout and a 1 s
  write timeout. If one device operation still runs after 5 s, a watchdog
  thread closes the port, marks the device absent (`device hung: port
  closed`), and the device thread reopens it. A dead USB/IP link after
  Windows sleep left `/dev/ttyACM0` in place and hung the device thread.
- **Port choice.** Without `--port`, every (re)open searches sysfs again for
  `0416:5011` nodes. When several match (a stale node next to the live one
  after a re-attach), each one is probed with `*IDN?` (1 s), and the first
  that answers is used. The skipped nodes are named in an event.
- **Fresh data only.** `status` and `daemon status` wait up to 3 s for a sample
  taken after the device was (re)opened. They exit 5 when the device shows as
  present but the newest sample is older than max(3 s, 5 poll periods).
- **Open report.** Each (re)open reads the state. If the output is ON at open,
  a warning event names the raw status byte and the setpoints.
- When a `set` fails part way (ISET took, VSET did not), the error and a
  `set-partial` audit record name the writes that took effect and the
  setpoints that the supply now holds.
- If the port disappears, the daemon reports `device absent` and tries to
  reopen it every 2 s. While the device is absent (or the port is busy),
  `korad status` exits 5 and marks the last-known values as stale.
- **The last output request wins.** An `out on` that reaches the device
  thread after a newer `out on` or `out off` was made, or that waited in the
  queue for more than 2 s, is dropped with exit 7 (`E_SUPERSEDED`). An
  `out off` is never dropped.
- **A request whose client has gone is not run** (except `out off`). A
  command that waits 15 s for the device thread is cancelled and not run.
  The client gives up after 20 s and says that the request may or may not
  have been applied.
- **The port is exclusive.** The daemon sets `TIOCEXCL`, so a second open of
  the tty by a normal user fails with EBUSY. A busy port is reported as
  `port busy`, not as `device absent`.
- **One daemon per supply.** The daemon holds an flock on
  `/tmp/korad-<usb serial or 0416-5011>.lock` (holding its pid and socket).
  A second daemon refuses to start and names the first one. `daemon stop`
  returns only after the lock, and so the port, is free.
- The poll rate is `max` or at least 1 Hz. A slower poll would delay the
  live guard check.
- The guard file is checked on load. A bad file stops the daemon at start;
  at `daemon reload` the reload fails and the previous guard stays.

## Guard

- A guard holds limits, an optional mode, an expiry and a note. Limits are
  global (both channels) or per channel. A quantity without a limit is not
  limited.
- The guard is checked:
  1. before every `VSET`/`ISET`, on the requested value
  2. after every write, on the read-back value
  3. before `OUT1`, on the read-back setpoints of both channels
  4. after a preset recall (the supply turns the output OFF on `RCL`). If the
     recalled values break the guard, the setpoints go to 0 V
     (`on_recall_violation = "zero"`)
  5. by the poller while the output is ON. A front-panel change that breaks
     the guard turns the output OFF (`on_live_violation = "off"`). The poller
     checks the **measured** VOUT on every sample (series: VOUT1 + VOUT2;
     parallel: VOUT2) against the voltage limit plus 0.02 V, so a knob turn
     is caught within one or two samples, not at the next setpoint poll. IOUT
     is not checked this way: turn-on peaks exceed the steady current.
- **OUT switches both channels.** So under an active guard, output ON is
  refused while a channel with no limit at all (no voltage and no current
  limit) is above 0 V. The poller applies the same rule while the output
  is ON.
- **Modes.** A move to independent is never refused: the output goes OFF, and
  the check before output ON still applies. Series and parallel are allowed
  only when the guard names that mode. In series, `max_v` is the total across
  the pair (2 × VSET2). In parallel, `max_i` is the total (2 × ISET2). A
  series or parallel guard needs global limits. A series guard with
  `max_v` ≤ 31 V, or a parallel guard with `max_i` ≤ 5.1 A, is refused as
  illogical.
- **With no guard, or an expired guard, nothing is blocked.** The tool prints a
  warning at daemon start, at monitor start, on each value increase and on
  each mode change.
- **Expiry** is one mechanism with three spellings: `--for 4h`,
  `--until 18:00`, and `--today`, which ends at the next 10:00 local time. The
  expiry is stored in UTC. `--for` needs a unit. A guard must last at least
  60 s. A `--today` window under 1 h prints a warning. The expiry follows the
  wall clock only. The poller reports a wall-clock jump of more than 60 s
  (wall time against monotonic time between two polls) with one `clock` event
  and a warning, and says when the jump expired the guard. Every guard display
  shows the expiry and the
  current time, both in local time with the zone and offset, and the time
  left.
- **Tightening needs no confirmation.** Loosening or clearing needs a typed
  word, and a wrong or empty answer keeps the current guard. The word is the
  new value itself (`12.00`), the mode name (`series`), the new expiry
  (`HH:MM`), `unlimited` for a removed limit, or `clear`. Non-interactive
  form: `--confirm "<words>"`. This catches a misclick or a cut input from a
  person. It does not stop an agent that copies the word, and it is not meant
  to.

## Identity and USB/IP re-attach

- **Identity** comes from the configuration file: `usb_id` (VID:PID, ASCII
  hex, stored lowercase; default `0416:5011`) and `usb_serial` (printable
  ASCII, no spaces, up to 64 characters; empty means any). `daemon start
  --usb-id/--usb-serial` override them for that daemon run, and a reload
  keeps the overrides. There is no environment variable for them.
- **Port choice.** Every (re)open scans `/sys/class/tty/ttyACM*` for
  `usb_id`. With `usb_serial` set, only that serial counts. With it empty and
  several devices of DIFFERENT serials present, the daemon refuses to pick
  (`E_DEVICE`, listing the serials). Several nodes of ONE serial (a stale
  node next to the live one) are probed with `*IDN?`, and the first that
  answers is used.
- **Re-attach (WSL only).** `usbipd_reattach` is `off` or `wsl`; the default
  is `wsl` when `/proc/sys/kernel/osrelease` contains "microsoft" and
  `usbipd.exe` is found (`usbipd_path`, else PATH, else the default install
  path), otherwise `off`. With no `--port`/`port`, when the device has been
  absent for more than 3 s, at most once per 10 s, a background thread (it
  never blocks the device thread) runs `usbipd.exe state` (JSON; fallback:
  `usbipd.exe list`, which carries no serial), picks the device by `usb_id`
  and `usb_serial` (never guessing between different serials), and:
  - shared, not attached: `usbipd.exe attach --wsl --busid <busid>` with a
    15 s timeout; event `usbipd re-attach: busid X attached` or the failure
    text
  - not shared: event asking a human to run `usbipd bind` once, elevated.
    The daemon never binds or elevates
  - attached to a client: event, and no attach. Blocked states are logged
    once and stay quiet until the state changes
  - not on the host bus: event, once per change

## CLI

```
korad status|s
korad set <1|2|12> [-v VOLTS] [-i AMPS]     # ISET first, then VSET, with read-back
korad out|o on|off
korad mode|m independent|series|parallel
korad preset|p <1-5>        korad preset-save|ps <1-5>
korad ovp on|off            korad ocp on|off
korad guard|g [show|set|clear]
korad monitor|mon [--csv FILE]
korad log|l -f FILE [--interval 1s|max] [--duration 10m] [--echo]
korad daemon|d start|stop|status|reload [--fake] [--foreground]
```

- Values: a plain number is volts or amps. Suffixes `V`, `mV`, `A`, `mA`.
  More precision than the supply takes (10 mV, 1 mA) is refused, not rounded.
  `3v3` is refused.
- `--json` prints one line: `{"ok", "command", "data", "warnings"}` or
  `{"ok": false, "command", "error": {"code", "message"}}`.
- Exit codes: 0 ok, 1 internal, 2 usage, 3 guard refusal, 4 read-back
  mismatch, 5 daemon or device not available, 6 confirmation missing or wrong,
  7 output ON dropped because a newer output request came first.
- `--json` can stand before or after the command, and every error path,
  including usage errors and internal errors, prints one JSON line.
- Input is ASCII only: other digit forms are refused. `M` means mega, so
  `3300MV` and `50MA` are refused. A repeated option (`-v 5 -v 3`) is refused.
  `--for` needs a unit and is at most 30 days.

## Monitor keys

Each action has a key for each hand.

| Action | Left hand | Right hand |
|---|---|---|
| value up / down | `d` / `f` | `k` / `j` |
| value up / down, ×10 step | `D` / `F` | `K` / `J` |
| select channel CH1 / CH2 | `a` / `s` | `h` / `l` |
| select field V ↔ I | `e` | `i` |
| step size fine / medium / coarse | `c` | `n` |
| output ON (press twice within 1 s) | `t` `t` | `y` `y` |
| output OFF (one press) | `space`, `x` | `space`, `p` |
| command line (`:v2 3.3`, `:i1 50m`, `:mode series`, `:q`) | | `:` |
| quit (the output stays as it is) | `q` | `:q` |

Steps: V 0.01 / 0.1 / 1.00, I 0.001 / 0.010 / 0.100. One key press changes a
value by at most 5.00 V or 0.500 A, so ×10 at the coarse step is capped. A held
key sends at most one write per 100 ms, always the latest value.

Output ON: the second press must come 150 ms to 1 s after the first, and ON
fires only when no third press follows within 120 ms. A held key (auto-repeat)
never turns the output ON.

## Files

| Path | Content |
|---|---|
| `~/.config/korad/config.toml` | behavior settings (see `config.example.toml`) |
| `~/.local/state/korad/guard.json` | the active guard. Only `korad guard` writes it |
| `~/.local/state/korad/audit.jsonl` | every write: client, request, read-back, guard result |
| `~/.local/state/korad/daemon.log` | daemon output |
| `~/.local/state/korad/korad.sock` | the daemon socket (mode 0600) |

`KORAD_HOME` moves all of these under one directory (tests use it).
