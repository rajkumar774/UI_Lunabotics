# Rover Mission Control — System Architecture

## 1. System Overview

The Rover Mission Control application is a competition-grade operator station interface for a planetary rover (Lunabotics competition). The system provides command, telemetry monitoring, safety management, visual awareness, and diagnostics for remote manual and autonomous operations.

```
+-------------------------------------------------------------+
|               Mission Control (Windows Laptop)              |
|                                                             |
|   +-----------------------------------------------------+   |
|   |             PySide6 GUI (Qt UI Thread)              |   |
|   +-----------------------------------------------------+   |
|                              | Qt Signals / Slots           |
|   +-----------------------------------------------------+   |
|   |         Communication Abstraction Layer             |   |
|   |          (BaseCommsClient Interface)                |   |
|   +-----------------------------------------------------+   |
|                 /                         \                 |
|   +--------------------------+   +----------------------+   |
|   | MockCommsClient (Worker) |   | ROS2CommsClient      |   |
|   |   (Standalone Dev Mode)  |   | (rclpy QThread)      |   |
|   +--------------------------+   +----------------------+   |
+----------------------------------------------|--------------+
                                               | Local Network
+----------------------------------------------|--------------+
|             Physical Rover (Jetson Orin Nano)               |
|                                                             |
|   +-----------------------------------------------------+   |
|   |         ROS 2 Humble Nodes (Ubuntu 22.04)           |   |
|   +-----------------------------------------------------+   |
|          |                 |                 |              |
|   +--------------+  +---------------+  +----------------+   |
|   | Livox MID-360|  | IMU & Encoders|  | Front Camera   |   |
|   +--------------+  +---------------+  +----------------+   |
+-------------------------------------------------------------+
```

---

## 2. Hardware and Environment Targets

- **Mission Control Laptop**:
  - OS: Windows 10/11
  - Runtime: Python 3.10+
  - UI Toolkit: PySide6 (Qt 6)
  - Role: Operator Station (visualization, safety interlocks, teleop commanding, telemetry logging).
- **Rover Onboard Computer**:
  - Hardware: NVIDIA Jetson Orin Nano
  - OS: Ubuntu 22.04 LTS
  - Middleware: ROS 2 Humble Hawksbill
  - Role: Autonomous flight control, motion planning, motor control, sensor acquisition, video encoding.
- **Network Link**:
  - High-speed local wireless / wired Ethernet link.

---

## 3. Communication Abstraction & Modes

> This section covers Mission Control's *internal* comms layer. For how the Jetson and the
> MCC laptop actually talk across the arena during a competition run — the separate control,
> telemetry, video and shell planes, the setup-window sequence, and the failure scenarios —
> see [`comms_architecture.md`](comms_architecture.md).

To enable agile development and testing on Windows laptops without requiring physical hardware or a local ROS 2 installation, all communications are routed through the `BaseCommsClient` abstraction.

### 3.1 Mode 1: Mock / Standalone Mode (Default Dev Backend)
- Active when running without ROS 2 or without physical rover.
- Deterministic simulation of rover heartbeat, connection state, and telemetry container.
- Guarantees zero runtime dependencies on `rclpy`.

### 3.2 Mode 2: ROS 2 Humble Mode (Implemented — Phase 4)
- Selected by `comms.backend` in `app_config.yaml` (`auto` / `mock` / `ros2`).
- Isolated behind `ROS2CommsClient`, which owns a `ROS2WorkerThread` (`QThread`).
- That thread creates a **private rclpy context** rather than using the global one, so the
  node cannot collide with any other rclpy usage in the process and tests can start and
  stop it repeatedly.
- `ROS_DOMAIN_ID` is exported from `rover_config.yaml` *before* the context initialises,
  because the domain is read at init and cannot be changed afterwards.
- The executor is spun in 50 ms slices interleaved with telemetry publication (20 Hz) and
  housekeeping (10 Hz: command queue drain + heartbeat watchdog), so interruption is
  always responsive.
- `rclpy` and every message package are imported **inside** `run()`, never at module
  scope, so importing the module on a host without ROS 2 is harmless.

#### 3.2.1 QoS policy
Subscriptions are `BEST_EFFORT`, publishers `RELIABLE`. This is the only combination that
cannot silently fail to match: a BEST_EFFORT subscriber accepts publishers of either
reliability, and a RELIABLE publisher is accepted by subscribers of either. A mismatch
presents as a topic that lists in `ros2 topic list` but never delivers a message, which is
very expensive to debug mid-competition.

#### 3.2.2 Inbound aggregation
Topics arrive at different rates (IMU 50 Hz, odometry 20 Hz, diagnostics 1 Hz). Rather than
emitting a `RoverState` per message, the worker folds each message into a private
accumulating state under a lock and emits an independent deep copy at a fixed
`telemetry.ros_publish_rate_hz`. This keeps the `BaseCommsClient` contract identical to the
mock client's and prevents a high-rate sensor from dominating the pipeline.

#### 3.2.3 Outbound commands
Commands are **queued**, not published from the Qt thread, and drained on the ROS thread at
10 Hz. Every DDS write therefore happens on the thread that owns the node. Each queue entry
carries an operator-facing name alongside the publisher kind, so a discrete action is
acknowledged as `DUMP` rather than as the transport's `mission_cmd` — matching what the
mock client reports, so the status block reads the same on both backends.

#### 3.2.4 Message conversion
All ROS-message→`RoverState` mapping lives in `comms/ros2/conversions.py` as pure functions
that read plain attributes off whatever they are handed. They import no rclpy and no
generated message class, which means the mapping rules are unit-testable on a machine with
no ROS 2 installed, and *what a message means* stays separate from the plumbing that
receives it.

> **Note on `diagnostic_msgs`**: rclpy maps the ROS `byte` type to a one-byte Python
> `bytes` object, so `DiagnosticStatus.WARN` is `b'\x01'`, not `1`. Coercing it numerically
> raises, which would quietly downgrade every warning and error to OK. `diagnostic_level()`
> handles this explicitly and is covered by tests.

### 3.3 Command Plane: Jetson SSH
Distinct from both modes above. Topic data travels over DDS; shell commands travel over an
SSH session (`comms/jetson_ssh.py`) rendered by the terminal panel. A real PTY is requested
so long-running launches stream, `Ctrl-C` reaches the foreground process, and curses
programs render. The SSH connect runs on its own thread because a TCP connect to an
unreachable Jetson blocks for the full timeout.

**Nothing read from the SSH session is ever written into `StateManager`.** The command
plane cannot become an accidental second source of rover state.

---

## 4. Threading Principles & Concurrency Model

1. **Qt Main Thread (GUI Thread)**:
   - Dedicated strictly to widget rendering, user interaction, animation, and handling Qt signals.
   - **Zero blocking operations**: Network calls, frame decoding, file I/O, and ROS spin loops are strictly forbidden in this thread.
2. **Communication Worker Thread**:
   - Manages ROS 2 callbacks or Mock telemetry timer.
   - Converts inbound message payloads into thread-safe `RoverState` updates and emits Qt signals to the UI.
   - Updates are written into the lock-guarded `StateManager`; the UI reads independent snapshots
     on the Qt thread at a capped refresh rate, so no widget ever touches shared mutable state.
3. **Video Decoding Worker Threads** (one per camera):
   - Ingest incoming video streams (RTSP / test pattern).
   - Decode frames into `QImage` in a background worker and push them to the UI thread via
     thread-safe signals and bounded buffers.
4. **ROS 2 Executor Thread** (`ROS2WorkerThread`):
   - Owns a private rclpy context and node; spins the executor in short slices.
   - Writes inbound messages into a lock-guarded accumulating `RoverState`; emits deep
     copies on a Qt signal at a fixed rate.
   - Drains the outbound command queue, so every DDS publish happens on this thread.
5. **SSH Threads** (`JetsonSSHConnection`):
   - One short-lived thread for the blocking TCP/auth connect.
   - One `QThread` per reachability probe, so auto-connect never waits on a socket.
   - One reader `QThread` **per shell**, draining that PTY channel into the UI.

Across all of them the rule is the same: **the Qt thread never blocks.** An unreachable
Jetson, a dead camera and a silent ROS graph each fail on their own thread while the
interface stays at full frame rate.

---

## 5. Subsystems (Current Foundation & Roadmap)

### 5.1 Telemetry & State Management (Implemented — Phase 3A)
- `RoverState`: Thread-safe data container storing:
  - Connection & heartbeat status
  - IMU orientation (roll, pitch, yaw)
  - Odometry & wheel speeds
  - Battery voltage, current, and state of charge
  - Drive and excavation motor states
  - Diagnostics and system alerts
  - Sensor group health (`SensorHealthData`: IMU / encoders / LiDAR)
  - Telemetry freshness (`telemetry_valid`, `last_telemetry_time`, `is_stale`)

#### Telemetry data path

```
Comms client (MockCommsClient today, ROS2CommsClient later)
      │  telemetry_received(RoverState)      full rate (mock: 20 Hz)
      ▼
TelemetryBridge (core/telemetry_bridge.py)
      │            ├── StateManager.apply_telemetry()   every packet
      │            ├── StateManager.set_connection_state() / update_heartbeat()
      │            └── invalidate_telemetry() on link loss
      ▼
StateManager (telemetry/state.py)   ← SINGLE SOURCE OF TRUTH
      │  get_snapshot(): independent deep copy; staleness evaluated at read time
      ▼
EventBus.telemetry_updated(RoverState)   throttled to ui_refresh_fps (default 30)
      ▼
MainWindow._on_telemetry_snapshot → TelemetryPanel.update_from_state()
```

Design rules enforced by this path:
1. **UI widgets never own rover state.** They render a snapshot handed to them and hold no
   authoritative values of their own.
2. **The UI never subscribes to a comms client for telemetry.** Only `MainWindow`'s connection
   badge listens to the client lifecycle; all values arrive via the EventBus.
3. **Ingest rate and repaint rate are decoupled.** Telemetry may arrive far faster than 30 Hz;
   the bridge's refresh timer caps repaints so a high-rate real feed cannot flood the UI thread.
4. **`StateManager` stays free of PySide6.** It is plain, lock-guarded Python, so it is unit
   testable and unchanged by the eventual ROS 2 swap.
5. **Telemetry packets cannot rewrite link or safety state.** `apply_telemetry()` merges only
   sensor payload blocks; connection state and E-Stop remain owned by the client lifecycle.

#### Control-plane state path (Phase 4)

Rule 5 has a consequence worth stating explicitly: because `apply_telemetry()` refuses to
copy `mission_mode`, `mission_phase` and `is_estopped`, a comms client cannot deliver those
by stuffing them into a telemetry packet. They travel on their own signals instead:

```
comms client
   ├── mission_mode_changed(str)   ──► StateManager.set_mission_mode()
   ├── mission_phase_changed(str)  ──► StateManager.set_mission_phase()
   └── estop_changed(bool)         ──► StateManager.set_estop()  + immediate publish
                                              │
                                              ▼
                                   EventBus.telemetry_updated  ──►  widgets
```

`MockCommsClient` emits these when it accepts a command (it stands in for the rover, so it
reports the resulting state back rather than letting the UI assume it). `ROS2CommsClient`
emits them from the rover's own `/mission/autonomy_mode`, `/mission/state` and
`/safety/estop` messages. Either way the widget renders a snapshot, never its own click.

E-Stop publishes a snapshot immediately rather than waiting for the next refresh tick,
because a safety state must not sit for up to 33 ms behind a frame cap.

#### Mock telemetry (development only)
`telemetry/mock_generator.py` provides `MockTelemetryGenerator`: deterministic, bounded,
time-varying values built from sine/ramp functions and a seeded jitter source. It exists purely
to develop and validate the UI — it models no rover subsystem, and is simply not instantiated
once a real telemetry source is connected.

#### Severity model
`Severity` (`NORMAL` / `WARNING` / `ERROR` / `OFFLINE` / `NO_DATA`) and the helper functions
`battery_severity`, `latency_severity`, `attitude_severity`, `sensor_severity`, and
`connection_severity` live alongside the state model so every widget applies identical display
rules. Severity is rendered through the Qt dynamic property `severity` in `stylesheet.qss` and
is always accompanied by explicit text, never signalled by colour alone.

### 5.2 Safety & E-Stop Architecture
- Emergency Stop (E-Stop) has top priority in command dispatch.
- Heartbeat watchdog monitors link status; triggers `LOST LINK` alert if connection drops for $>1000\text{ ms}$.
- Teleoperation dead-man's switch: Commands expire after $500\text{ ms}$ without active operator input.

### 5.2b Operator Telemetry UI (Implemented — Phase 3A)
- `ui/components/telemetry_panel.py` assembles five compact cards into a height-capped strip so
  the live camera keeps the majority of the viewport.
- Reusable components: `PowerWidget`, `MotionWidget`, `AttitudeWidget`, `SensorStatusWidget`,
  `ConnectionStatusWidget`, built on shared primitives (`TelemetryCard`, `MetricRow`, `SensorRow`,
  `StatusDot`, `BatteryBar`) in `ui/components/telemetry_primitives.py`.
- Every card implements the same two-method contract: `update_from_state(RoverState)` and
  `show_no_data()`, so missing, stale, or malformed telemetry degrades safely instead of crashing.

### 5.2c Operator Control Column (Implemented — Phase 4)
- `ui/components/control_sidebar.py` stacks three blocks down the right-hand pane, in the
  order an operator works through them: choose a mode, dispatch an action, read the result,
  drop into a shell when something needs starting on the Jetson.
- Operator buttons resolve through `mission_actions:` bindings in
  `rover_config.yaml`, dispatched by `comms/mission_actions.py`. A button's
  destination — mock, SSH, topic, service or action — is a config value, so
  wiring a real command is a YAML edit rather than a UI change. Anything
  unparseable falls back to `mock`: a typo must never become a live rover
  command.
- Mock dispatches are **labelled as mocks** in both the panel caption and the
  status line. An operator must never be able to mistake a rehearsal for a real
  command.
- `AutonomyControlWidget` offers **actions** only — one-shot dispatches (excavate, dump,
  one cycle). There are deliberately **no mode-selection buttons**: the rover owns its
  control mode, and Mission Control displays it rather than setting it. The publisher on
  `/mission/autonomy_mode` remains available on the client API for a later phase.
- The widget owns no rover state. It emits requests; `MainWindow` forwards them to the
  comms client, and what the rover reports back is what `CommandStatusWidget` renders.
- An engaged E-Stop disables every action button but leaves itself pressable. A link loss
  disables commands but **not** E-Stop: an operator hitting it during a dropout should
  queue the command rather than find a dead button. Re-enabling on reconnect does not
  override a latched stop.
- `CommandStatusWidget` gates control mode and E-Stop on the **link**, not on telemetry
  freshness, because they are control-plane values written through `StateManager`'s own
  setters rather than carried on the telemetry stream. When linked but stale, E-Stop reads
  `UNCONFIRMED` instead of `CLEAR`; an *engaged* stop always displays, even on stale data,
  because hiding one is unsafe while showing a released one is merely wrong.

### 5.3 Live Camera Subsystem (Implemented — Phase 3/4)
- 16:9 aspect-ratio video player with YouTube-live style HUD.
- Live stream indicator, FPS/latency tracker, resolution metrics.
- Seamless automatic reconnection logic on packet drops.
- **Two independent pipelines** (Phase 4): the main stream fills the left pane and the
  secondary stream is drawn as a picture-in-picture inset inside the same 16:9 viewport —
  so the inset tracks the letterboxed video rather than the widget rectangle and never
  floats out over a black bar. Each camera has its own `VideoWorker` and `BoundedFrameBuffer`,
  so a stall on one cannot hold up the other. A dead inset shows `NO SIGNAL` rather than
  holding its last frame.
- Either stream falls back to `TestPatternVideoSource` when its `uri` is blank, which keeps
  the player usable with no rover attached.

#### 5.3.1 Backend selection is explicit, never inferred
`OpenCVVideoSource` accepts three input forms and **names the OpenCV backend for
each one**:

| `uri` form | Backend |
| :--- | :--- |
| `rtsp://host:port/path` | `CAP_FFMPEG`, with low-latency flags |
| `src ! ... ! appsink` | `CAP_GSTREAMER` |
| `0`, `/dev/video0`, a file | `CAP_ANY` |

> This is a bug fix, not a refactor. Passing a GStreamer pipeline to
> `cv2.VideoCapture()` without naming a backend let OpenCV auto-select
> GStreamer and then **segfault on the failure path** — the whole UI vanished
> when the operator pressed START STREAM with the rover unreachable.

A pipeline that does not end in `appsink` is refused at open with an explicit
error. OpenCV cannot read frames from `autovideosink` or `fpsdisplaysink`, and
failing loudly beats a capture that opens and never produces a frame.

#### 5.3.2 Capture construction is serialised
`cv2.VideoCapture` construction is guarded by a process-wide lock
(`_CAPTURE_OPEN_LOCK`). OpenCV's GStreamer backend is not safe to open from two
threads at once, and the main and PiP workers do exactly that — both entering
the failure path simultaneously is what produced the crash above.

Only the *open* is serialised; the two cameras still stream fully in parallel.
The cost is a few hundred milliseconds, once per connection attempt.

#### 5.3.3 Operator-side viewers
Separately from the in-UI player, `video/viewer_launcher.py` runs the GStreamer
pipelines listed in `video_launch:` as local subprocesses. These are receiver
pipelines that open their own windows, read from plain text files on every
press, and are started in their own process session so stopping kills the whole
`gst-launch` tree rather than orphaning children that hold the camera.

### 5.4 Spatial & LiDAR Subsystem (Roadmap)
- Single **Livox MID-360** (`/livox/lidar`), a 360 deg non-repetitive-scan unit with a
  built-in IMU on `/livox/imu`. Not four perimeter scanners: one sensor, consumed by
  FAST-LIO for odometry and by the crater/rock detectors for obstacles.
- 2D proximity radar / footprint clearance visualizer.
- **Needs a home in the layout.** The 2D map placeholder previously occupied the right
  pane, which is now the operator control column. The LiDAR view will need either a tab,
  a second inset, or a detachable window when real pointcloud data exists.

### 5.6 Jetson Terminal (Implemented — Phase 4)
- `ui/components/terminal_view.py` renders a [pyte](https://pypi.org/project/pyte/) screen
  model with `QPainter`. pyte owns the screen state, so this module knows nothing about
  escape sequences — which is why progress bars, coloured `ros2` output and curses programs
  all render without special cases.
- Rows are painted as runs of identical styling rather than cell by cell; a per-cell
  `drawText` over an 80x30 grid is slow enough to be visible when a node logs at speed.
- Incoming bytes go through an **incremental** UTF-8 decoder, because the network can split
  a multi-byte character across two reads.

#### 5.6.1 One connection, many shells
`comms/jetson_ssh.py` mirrors how SSH actually works: a `JetsonSSHConnection` (one socket,
one authentication) hands out any number of `JetsonShell` objects, each its own PTY
channel with its own reader `QThread`.

This exists to solve a concrete operational problem. A single shell runs one foreground
process at a time, so starting `ros2 launch` in it removes the prompt for the rest of the
run and any further keystrokes go to the launch rather than to a shell. The panel
therefore presents tabbed shells, and routes a dispatched command to one that is free —
opening a new tab if the current shell is busy, and refusing with a message if every shell
is busy. A command is **never** typed into a running process.

#### 5.6.2 Knowing when a shell is busy
Each shell is started with a `PROMPT_COMMAND` that emits an OSC 0 title sequence carrying a
sentinel before every prompt. OSC 0 is consumed by the emulator and never printed, so this
is the remote shell *telling* the UI it has returned to a prompt. A `pyte` screen subclass
overrides `set_title` to catch it.

The alternative approaches are all worse: a timer guesses, and pattern-matching the prompt
text breaks on any custom `PS1`. This one stays correct for a command that produces no
output for ten minutes, which is exactly the case that matters for `ros2 launch`.

`shell_init_commands()` builds the per-shell setup as a pure function so the ordering —
sentinel first, then the operator's `startup_commands`, then an optional `clear` — is
testable without an SSH server.

#### 5.6.3 Auto-connect
With `jetson_ssh.auto_connect`, the connection runs a timer that fires a one-shot
background TCP probe at the Jetson's SSH port, and connects as soon as the port answers.
That makes the terminal attach by itself when the rover boots, and re-attach after a
dropout. The probe is a `QThread` because a connect to a down host blocks for the full
timeout.

An operator pressing DISCONNECT stops the watcher, so a deliberate disconnect is not
undone a second later.

The panel's status is mirrored onto the header's **JETSON** badge, kept separate from
**ROVER LINK** on purpose: the DDS topic link and the SSH command shell fail
independently, and an operator needs to know which one is down.

- The panel degrades with an explanation rather than an empty box when disabled in config
  or when paramiko is missing.

### 5.5 Mission Timer & Competition Operations (Implemented — Phase 3B)
- Official competition run duration: **25 minutes** ($1500\text{ seconds}$ -- Guidebook v3,
  15 Oct 2026, section 1.3; shortened from 30), read from
  `mission.run_duration_seconds`.
- `core/mission_clock.py` — `MissionClock` counts the run down to zero. Remaining time is
  computed from a monotonic deadline rather than accumulated ticks, so the display cannot
  drift; ticks are emitted only when the displayed second changes. The clock is operator
  station time, not rover telemetry, so it is deliberately **not** routed through
  `StateManager`.
- On expiry the header shows **MISSION COMPLETE** and the timeline closes out at `POST-RUN`.
- `MissionPhase` (Pre-Check, Teleop, Autonomous, Dump, Post-Run) is an ordered label held in
  `StateManager` and rendered from snapshots, consistent with every other state value. It is
  intentionally **not** a state machine: there are no transition rules or guards, because
  nothing in the current scope requires them. The rover will eventually report this field on
  `/mission/state`; until then `MissionControlsWidget` provides development mock controls.
- `MissionPhase` (run timeline) is distinct from `MissionMode` (rover control mode, shown as
  `CTRL MODE` in the Rover Link card).
