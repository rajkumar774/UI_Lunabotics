<<<<<<< HEAD
# Rover Mission Control Application

A competition-grade operator interface for a Lunabotics-style planetary rover.

> **Status**: **Phase 4 — Live ROS 2 Link, Operator Controls & Jetson Terminal**.
> Mission Control now has a working ROS 2 Humble backend (`ROS2CommsClient`), a dual-camera
> video layout with a picture-in-picture inset, an operator control column (drive mode,
> mission actions, E-Stop), a control status block, and an embedded SSH terminal on the Jetson.
>
> **Two independent paths to the rover**, and the distinction matters:
>
> | Path | Carries | Transport |
> | :--- | :--- | :--- |
> | **Data** | Telemetry, odometry, IMU, diagnostics, and all commands | ROS 2 topics over DDS, discovered by shared `ROS_DOMAIN_ID` |
> | **Command shell** | `ros2 launch`, `systemctl`, `jtop`, reboots | SSH session to the Jetson, shown in the UI terminal |
>
> ⚠️ **Mock mode is still the default** (`comms.backend: auto` + `app.mock_mode: true`).
> In mock mode every telemetry value comes from `MockTelemetryGenerator` and is **not a
> measurement**. See [§4c](#4c-connecting-to-the-real-rover-ros-2) to switch to the live link.

---

## 1. System Overview & Architecture

The Mission Control application runs on a Windows operator laptop and provides command, telemetry monitoring, safety management, visual awareness, and diagnostics for remote manual and autonomous rover operations.

```
+-----------------------------------------------------------------------------+
|                  Mission Control (Operator Laptop, Ubuntu 22.04)            |
|                                                                             |
|   +---------------------------------------------------------------------+   |
|   |                  PySide6 GUI Interface (Qt UI Thread)               |   |
|   |   LEFT                           |  RIGHT (ControlSidebar)          |   |
|   |   LiveVideoPlayerWidget          |  - AutonomyControlWidget         |   |
|   |    - main 16:9 stream (D456)     |      mode + actions + E-Stop     |   |
|   |    - PiP inset (front camera)    |  - CommandStatusWidget           |   |
|   |   BOTTOM                         |  - JetsonTerminalWidget (SSH)    |   |
|   |   TelemetryPanel (5 cards)       |                                  |   |
|   +---------------------------------------------------------------------+   |
|        ^                    |                      ^              ^         |
|        | EventBus           | commands             | frames       | PTY     |
|        |  (throttled 30 FPS)|                      |              | bytes   |
|   +----------------------+  |         +---------------------+  +---------+  |
|   | StateManager         |  |         | VideoWorker x2      |  | Jetson  |  |
|   |  (source of truth)   |  |         |  (QThread each)     |  | SSH     |  |
|   |  <- TelemetryBridge  |  |         |  - bounded buffer   |  | Session |  |
|   +----------------------+  |         |  - stats tracker    |  | (thread)|  |
|        ^                    v         +---------------------+  +---------+  |
|   +---------------------------------+          ^                    ^       |
|   |    Communication Abstraction    |          |                    |       |
|   |    - MockCommsClient (default)  |          |                    |       |
|   |    - ROS2CommsClient  --------. |          |                    |       |
|   +-------------------------------|-+          |                    |       |
|                                   v            |                    |       |
|                        ROS2WorkerThread (rclpy node)                |       |
+-----------------------------------|------------|--------------------|-------+
                                    | DDS        | RTSP/UDP           | SSH
                       same ROS_DOMAIN_ID        |                    | :22
+-----------------------------------|------------|--------------------|-------+
|                  Physical Rover (NVIDIA Jetson Orin Nano)                   |
|                  - ROS 2 Humble / Ubuntu 22.04                              |
|                  - GStreamer Hardware Encoder (nvv4l2h264enc)               |
|                  - MediaMTX RTSP Server (H.264 @ 720p 30 FPS)               |
|                  - sshd (operator shell)                                    |
+-----------------------------------------------------------------------------+
```

### Key Architectural Principles:
1. **Operator Interface, Not Robot Brain**: The GUI does not directly command hardware or run low-level motor loops. It issues high-level setpoints to onboard ROS 2 nodes.
2. **Communication Abstraction**: All communication flows through `BaseCommsClient`. The default `MockCommsClient` enables full GUI development without ROS 2 or physical hardware.
2b. **Single Source of Truth**: Telemetry never travels from a comms client straight into a widget. It flows `comms client → StateManager → EventBus → telemetry widgets`, so replacing the mock client with a real ROS 2 client requires no UI change.
2c. **Data Plane and Command Plane Are Separate**: Topic data arrives over DDS; shell commands go over SSH. Nothing read from the SSH session is ever written into `StateManager`.
2d. **The UI Renders What the Rover Reports**: Pressing a mode button dispatches a request. The button's selected state comes back from the rover's own report, not from the click, so the interface can never show a mode the rover is not actually in.
3. **Dedicated Video Pipeline & Threading**: Video ingestion and frame decoding run in a dedicated `VideoWorker` (`QThread`), completely isolated from the Qt UI thread. Each camera gets its own worker and buffer, so a stall on one cannot hold up the other.
4. **Stale Frame Dropping**: The `BoundedFrameBuffer` (capacity: 2) drops older frames when processing lags, prioritizing lowest operator latency over displaying every stale frame.
5. **YouTube-Live Style Player**: 16:9 aspect ratio locking, 🔴 LIVE status indicator, real-time FPS/latency display, resolution badges, fullscreen mode, and seamless reconnect states.
6. **Nothing Blocks the Qt Thread**: rclpy spinning, video decode, SSH connect and SSH reads each run on their own thread. An unreachable Jetson times out in the background while the UI stays at full frame rate.

---

## 2. Prerequisites

- **Python**: `3.10` or higher
- **OS**: Ubuntu 22.04 for the live ROS 2 link; Windows 10/11 or macOS work for mock-mode UI development
- **ROS 2 Humble** (optional, required only for the live link): `rclpy` plus `sensor_msgs`,
  `nav_msgs`, `geometry_msgs`, `std_msgs`, `diagnostic_msgs`. These come from the ROS 2
  installation, not from pip — `source /opt/ros/humble/setup.bash` before launching.
  Without them the application starts normally in mock mode.

---

## 3. Setup & Installation

### Step 1: Clone the Repository
```powershell
git clone <repository_url>
cd US_ui
```

### Step 2: Create a Local Virtual Environment
```powershell
python -m venv .venv
```

### Step 3: Activate the Virtual Environment
- **Windows (PowerShell)**:
  ```powershell
  .\.venv\Scripts\Activate.ps1
  ```
- **Windows (Command Prompt)**:
  ```cmd
  .\.venv\Scripts\activate.bat
  ```

### Step 4: Install Dependencies
```powershell
pip install -e .
# Or install development dependencies:
pip install -e ".[dev]"
```

*Dependencies:* `PySide6`, `PyYAML`, `numpy`, `opencv-python`, `paramiko` (Jetson SSH),
`pyte` (terminal screen model), `pytest`.

On Ubuntu with ROS 2, create the virtual environment with `--system-site-packages` so it
can see the apt-installed `rclpy`:

```bash
source /opt/ros/humble/setup.bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

---

## 4. Running the Application

To launch the Mission Control interface in standalone development mode:

```powershell
python -m mission_control.main
```
Or via the entrypoint script:
```powershell
mission-control
```

- Click **START STREAM** on the video player to begin rendering the live 16:9 synthetic test pattern (radar sweep line, dynamic frame counter, millisecond timestamp, and simulated latency). This starts both the main stream and the picture-in-picture inset.
- Click **⛶ FULLSCREEN** (or press `Esc`) to enter and exit fullscreen video mode.
- Click **CONNECT MOCK** in the top header to start the mock link, heartbeat, and telemetry stream. The telemetry strip along the bottom begins updating immediately; clicking **DISCONNECT** returns every card to its `--` / `NO DATA` state.
- The right-hand column carries the operator controls and the Jetson shell — see [§4d](#4d-operator-control-column) and [§4e](#4e-jetson-terminal).

### Window layout

```
┌──────────────────────────────────────────────────────────────┐
│ ROVER MISSION CONTROL   [ROVER LINK][JETSON] 29:58 ▶ ↺        │
│                         [SAFETY][CONNECT][E-STOP]            │
├─────────────────────────────────────┬────────────────────────┤
│ MAIN CAMERA (D456), 16:9 locked     │ ACTIONS                │
│                                     │  EXCAVATE | DUMP       │
│                                     │  ONE CYCLE AUTO        │
│                                     │  EMERGENCY STOP        │
│                                     ├────────────────────────┤
│                                     │ CONTROL STATUS         │
│                       ┌───────────┐ ├────────────────────────┤
│                       │ FRONT CAM │ │ JETSON TERMINAL        │
│                       │   (PiP)   │ │ (●sh1)(sh2)  [+ SHELL] │
├───────────────────────┴───────────┴─┴────────────────────────┤
│ POWER │ MOTION │ ATTITUDE │ SENSORS │ ROVER LINK             │
└──────────────────────────────────────────────────────────────┘
```

Two things were removed from the earlier layout:

- **The mission phase row and header badge.** The clock's start/pause/reset moved inline
  beside the countdown, so the window has one header row instead of two and the camera
  gets the height back. `MissionPhase` still exists in `RoverState` and is still written by
  the rover on `/mission/state` — it simply has no widget.
- **The 2D LiDAR map placeholder**, to make room for the control column. That subsystem is
  still on the roadmap ([`docs/architecture.md` §5.4](docs/architecture.md)) and needs a
  new home in the layout when real pointcloud data exists.

The front camera inset sits in the **bottom-right** corner of the main viewport
(`cameras.pip.corner`).

---

## 4c. Connecting to the real rover (ROS 2)

The operator laptop and the Jetson each run ROS 2 Humble. Set the **same `ROS_DOMAIN_ID`**
on both and DDS discovers the topics automatically — there is no broker and no IP address
to configure for topic data.

### Step 1 — match the domain

`config/rover_config.yaml`:
```yaml
rover:
  ros_domain_id: 17    # must equal ROS_DOMAIN_ID on the Jetson
```

Mission Control exports this into the environment before `rclpy` initialises its context,
so it does not matter whether your shell already had `ROS_DOMAIN_ID` set.

### Step 2 — select the ROS 2 backend

`config/app_config.yaml`:
```yaml
app:
  mock_mode: false     # stop generating fake telemetry
comms:
  backend: ros2        # auto | mock | ros2
```

| `comms.backend` | Behaviour |
| :--- | :--- |
| `auto` *(default)* | ROS 2 when `rclpy` imports **and** `app.mock_mode` is `false`; otherwise mock. |
| `mock` | Always `MockCommsClient`. No DDS traffic at all. |
| `ros2` | Always `ROS2CommsClient`. Logs an error and falls back to mock if `rclpy` is missing, so the station still starts. |

### Step 3 — launch with ROS 2 on the path

```bash
source /opt/ros/humble/setup.bash
python -m mission_control.main
```

### Topics consumed and published

Subscriptions are **BEST_EFFORT** and publishers **RELIABLE**, deliberately: a BEST_EFFORT
subscriber matches publishers of either reliability and a RELIABLE publisher matches
subscribers of either. That combination avoids silent QoS incompatibility — the failure
where a topic appears in `ros2 topic list` but never delivers a message.

| Direction | Topic (configurable) | Type | Maps to |
| :--- | :--- | :--- | :--- |
| ← sub | `/imu/data` | `sensor_msgs/Imu` | Roll / pitch / yaw (quaternion → degrees, yaw wrapped to 0–360) |
| ← sub | `/odom` | `nav_msgs/Odometry` | Ground speed, turn rate |
| ← sub | `/joint_states` | `sensor_msgs/JointState` | Wheel RPM per side (rad/s → RPM), excavation drum and arm |
| ← sub | `/battery_state` | `sensor_msgs/BatteryState` | Charge (0–1 → %), voltage, draw (sign-corrected), cell temp |
| ← sub | `/diagnostics` | `diagnostic_msgs/DiagnosticArray` | Jetson CPU/GPU/RAM/temp, sensor health, active alerts |
| ← sub | `/rover/heartbeat` | `std_msgs/Header` | Link watchdog and latency |
| ← sub | `/mission/state` | `std_msgs/String` | Mission phase |
| ← sub | `/mission/autonomy_mode` | `std_msgs/String` | Control mode shown in the UI |
| ← sub | `/safety/estop` | `std_msgs/Bool` | Latched E-Stop (rover is authoritative) |
| → pub | `/cmd_vel` | `geometry_msgs/Twist` | Drive setpoints |
| → pub | `/safety/estop` | `std_msgs/Bool` | E-Stop engage / release |
| → pub | `/mission/autonomy_mode` | `std_msgs/String` | Mode request (API only; no UI control) |
| → pub | `/mission/cmd` | `std_msgs/String` | `EXCAVATE` / `DUMP` / `ONE CYCLE AUTO` |

Every topic name is overridable under `ros_topics:` in `config/rover_config.yaml`. The
message *types* are fixed; if the rover publishes different types, the mapping functions in
[`comms/ros2/conversions.py`](src/mission_control/comms/ros2/conversions.py) are the only
place that needs changing.

### Link state

There is no handshake in DDS, so the link is defined by the heartbeat:

| Condition | State |
| :--- | :--- |
| Node started, no heartbeat yet | `CONNECTING` |
| Heartbeat received | `CONNECTED` |
| No heartbeat for `mission.heartbeat_timeout_ms` (default 1000 ms) | `LOST LINK`, sensor health reset to `UNKNOWN` |

Latency is derived from the heartbeat `Header.stamp` and is only meaningful when both
clocks are NTP/PTP synchronised. A rover clock running ahead reads `0.0 ms` rather than a
negative value.

### Cameras

Two independent ingest pipelines, each with its own worker thread and bounded buffer:

```yaml
cameras:
  main:                                      # full-size left pane
    name: "D456 DEPTH CAMERA"
    uri: "rtsp://192.168.1.100:8554/d456"    # blank => synthetic test pattern
  pip:                                       # inset in the main viewport
    name: "FRONT CAM"
    uri: "rtsp://192.168.1.100:8554/front_camera"
    corner: "top_right"                      # top_left | top_right | bottom_left | bottom_right
    width_fraction: 0.26                     # clamped to 0.12–0.50
```

With `uri` blank, that stream falls back to the synthetic test pattern, so the player stays
usable with no rover attached.

---

## 4d. Operator control column

| Block | Contents |
| :--- | :--- |
| **SYSTEMS** | `LOCALIZATION` — subsystem bring-up, pressed once during setup |
| **ACTIONS** | `EXCAVATE`, `DUMP`, `ONE CYCLE AUTO` — mission tasks |
| **EMERGENCY STOP** | Latching. Becomes `RELEASE E-STOP`; locks out every other button while engaged. |
| **CONTROL STATUS** | `CTRL MODE`, `E-STOP`, `LAST COMMAND`, `COMMAND LINK` |

There are no mode-selection buttons: the rover owns its control mode, and
Mission Control displays it under `CTRL MODE` rather than setting it.

### Where a button's real command lives

Every button resolves through `mission_actions:` in `config/rover_config.yaml`.
Changing what a button does is a config edit, not a code change:

```yaml
  LOCALIZATION:
    is_system: true
    mode: ssh                 # mock | ssh | topic | service | action
    command: "ros2 launch rover_localization localization.launch.py"
```

| `mode` | Does | Status |
| :--- | :--- | :--- |
| `mock` | Acknowledges, commands nothing | current for all ACTIONS |
| `ssh` | Runs `command` in the Jetson terminal | **works today** |
| `topic` | Publishes `payload` as `std_msgs/String` | works today |
| `service` | Calls `LaunchSystem.srv` | needs the Jetson service |
| `action` | Sends an action goal | needs the action servers |

`ssh` is a deliberate **bridge**, not the destination. It is acceptable for
subsystem bring-up — pressed once, during setup, with the launch output
visible — but not for run-time tasks, which need a structured result and a
cancel handle. Those become `action` when the servers exist. The two
`_dispatch_*` methods in
[`comms/mission_actions.py`](src/mission_control/comms/mission_actions.py)
are the only code to write; they carry the rclpy snippets in their docstrings.

**Mocks are visibly mocks.** The ACTIONS caption reads amber
`ACTIONS · MOCK — NO ROVER COMMAND`, and the status line reads
`EXCAVATE (MOCK) · SENT`. An operator must never mistake a rehearsal for a
dispatch.

**Cameras are deliberately not buttons.** Video is worth 240 points across two
runs and must return unaided after the E-Stop cold-reboots the Jetson — so it
belongs in systemd at boot, which no button can provide.

Two behaviours worth knowing:

- **The two E-Stops are one control.** The header button and the column's
  `EMERGENCY STOP` drive the same command and presentation.
- **E-Stop stays live when the link drops.** Everything else greys out; E-Stop
  does not, because an operator hitting it during a dropout should queue the
  command rather than find a dead button.

When telemetry goes stale while the link is up, `E-STOP` reads
**`UNCONFIRMED`** rather than `CLEAR`. An engaged stop always displays, even on
stale data: hiding one is unsafe, showing a released one is merely wrong.

---

## 4f. Video viewers

`START STREAM` does two things:

1. starts the in-UI players (`cameras.main` / `cameras.pip`), and
2. launches the operator-side viewer pipelines listed under `video_launch:`.

```yaml
video_launch:
  enabled: true
  stop_on_stream_stop: true
  viewers:
    - name: "D456 FRONT"
      command_file: "~/frontcamera.txt"
    - name: "BACK CAM"
      command_file: "~/backcamera.txt"
```

Those files are **re-read on every press**, so a pipeline can be tuned between
attempts with no restart. Each viewer runs in its own process session, so
`STOP STREAM` kills the whole `gst-launch` tree rather than orphaning children
that keep the camera open.

The two paths render differently and that is the point of having both:

| | Renders | Pipeline must end in |
| :--- | :--- | :--- |
| `video_launch` viewers | their own GStreamer windows | `autovideosink` / `fpsdisplaysink` |
| `cameras.*.uri` (in-UI) | the 16:9 pane and the PiP inset | **`appsink`** |

> **`appsink` is not optional for the in-UI path.** OpenCV cannot read frames
> from a display sink. A pipeline ending in `autovideosink` is refused at open
> with an explicit error rather than left to hang.

## 4e. Jetson terminal

Real interactive SSH shells on the rover, embedded in the right-hand column. Topic data
does **not** travel through them — this exists because starting nodes, inspecting the graph
with the `ros2` CLI, checking `jtop` and rebooting all need a shell.

Each shell is a true PTY (`paramiko.invoke_shell`) rendered through a
[pyte](https://pypi.org/project/pyte/) VT100 screen model, so long-running `ros2 launch`
output streams live, `Ctrl-C` reaches the foreground process, progress bars that rewrite
their line behave, and curses programs render.

### Running a command while another is already running

A single shell runs **one foreground process at a time**. Start `ros2 launch` in it and the
prompt is gone for the rest of the run — typing anything just feeds keystrokes to the
launch. So the panel does not use one shell:

> **One SSH connection, several shells.** Each tab is its own PTY channel on the same
> authenticated connection. A command fired while the current shell is busy **opens a new
> tab instead of being typed into the running process.**

```
┌──────────────────────────────────────────────┐
│ [TOPIC LIST] [NODE LIST] [BRINGUP] …         │   quick commands
├──────────────────────────────────────────────┤
│ ( ● sh1 )( sh2 )( sh3 )            [+ SHELL] │   ● = still running
├──────────────────────────────────────────────┤
│ jetson@orin:~$ ros2 launch rover_bringup …   │
│ [INFO] [camera]: publishing 1280x720@30      │
└──────────────────────────────────────────────┘
```

| Situation | What happens |
| :--- | :--- |
| Current shell is at a prompt | The command runs there. |
| Current shell is busy | A new tab opens and the command runs in it; the busy shell is untouched. |
| The busy shell later finishes | Its `●` clears and it is reused for the next command. |
| No shell open yet | One is opened. |
| All 6 shells busy | The command is **refused with a message** — never typed into a running process. |

**How "busy" is known.** Each shell is started with a `PROMPT_COMMAND` that emits an OSC
title sequence before every prompt. OSC 0 is consumed by the emulator and never printed, so
this is the shell telling the UI "I am back at a prompt" — not a timer, not a guess at the
prompt's text. It stays correct for a command that prints nothing for ten minutes.

`+ SHELL` opens one by hand at any time (up to 6). `^C` and `CLEAR` act on the **active tab
only**, and closing a tab closes that shell.

### Auto-connect

With `auto_connect: true` (the default), the panel probes the Jetson's SSH port on a timer
and attaches by itself as soon as the rover boots — no operator action. If the link later
drops, the same watcher re-attaches. The header **JETSON** badge tracks it:

| Badge | Meaning |
| :--- | :--- |
| `WAITING` (amber) | Auto-connect is watching; the Jetson is not up yet. |
| `CONNECTING` (amber) | Port is open, authenticating. |
| `CONNECTED` (green) | Shells are available. |
| `OFFLINE` (red) | Not connected and not watching (operator pressed DISCONNECT). |

Pressing **DISCONNECT** stops the watcher, so an operator who deliberately disconnects is
not reconnected a second later. The probe runs on its own thread; an unreachable Jetson
never blocks the UI.

`JETSON` is deliberately separate from `ROVER LINK` in the header: the topic link and the
command shell fail independently, and an operator needs to know which one is down.

```yaml
jetson_ssh:
  enabled: true
  host: "192.168.1.100"        # blank falls back to network.rover_ip
  port: 22
  username: "jetson"
  key_path: "~/.ssh/id_rsa"    # blank uses ssh-agent, then password auth
  auto_connect: true           # attach as soon as the Jetson boots
  reconnect_interval_s: 5      # probe / retry cadence
  clear_on_open: true          # clear each new shell after startup commands
  startup_commands:
    - "source /opt/ros/humble/setup.bash"
  quick_commands:
    - label: "TOPIC LIST"
      command: "ros2 topic list"
```

| Control | Behaviour |
| :--- | :--- |
| **CONNECT / DISCONNECT** | Opens or closes the connection. Disconnect also stops auto-reconnect. |
| **+ SHELL** | Opens another shell tab on the same connection. |
| **^C** | Sends `Ctrl-C` (ETX) to the active tab's foreground process. |
| **CLEAR** | Clears the active tab's screen and scrollback. |
| Quick commands | One-click buttons from `quick_commands`, routed per the table above. |
| `Shift+PageUp` / `PageDown`, wheel | 2000 lines of local scrollback. |
| `Ctrl+Shift+C` / `Ctrl+Shift+V` | Copy the visible screen / paste. |

The terminal degrades honestly: `jetson_ssh.enabled: false` disables the panel with a note,
and a missing `paramiko` shows an install hint instead of an empty box. Host keys use
`AutoAddPolicy` — the rover is a known device on a private competition LAN whose key changes
whenever the Jetson is reflashed, and refusing to connect mid-run would be worse.

---

## 4a. Mission Clock

The header carries the run countdown as the largest element in the window, with its
controls inline:

```
MISSION CLOCK
   29:59      ▶  ↺
```

- **Countdown** — counts down from `mission.run_duration_seconds` (1800 s / `30:00` by
  default) in `config/app_config.yaml`. Remaining time is derived from a monotonic
  deadline, so a late timer callback cannot make the clock drift.
- **Severity** — cyan while normal, amber under 5 minutes, red under 1 minute.
- **MISSION COMPLETE** — at `00:00` the caption switches from `MISSION CLOCK` to a red
  **MISSION COMPLETE** and the clock stops.
- **Controls** — `▶`/`❚❚` starts and pauses, `↺` resets to the full duration. The clock is
  operator station time, not rover telemetry, so these command nothing on the rover.

### Mission phase

**There is no mission phase UI.** The phase row and the header badge were removed. The
`MissionPhase` field still exists in `RoverState`, is still written by the rover on
`/mission/state`, and still closes out at `POST-RUN` when the clock expires — it just has
no widget rendering it. Re-adding a display later needs only a widget; the data path is
already there.

---

## 4b. Telemetry UI (Phase 3A)

The bottom strip shows five compact cards. All values are **MOCK DATA**.

| Card | Contents |
| :--- | :--- |
| **POWER** | Battery percentage + segmented charge bar, pack voltage, current draw, derived power state (`NORMAL` / `LOW` / `CRITICAL`). |
| **MOTION** | Ground speed (m/s), mean wheel RPM, left/right wheel split, turn rate. |
| **ATTITUDE** | Roll, pitch (signed, with tilt severity), and heading. |
| **SENSORS** | IMU / encoder / LiDAR health with dot **and** text label, plus a Jetson temperature and CPU line. |
| **ROVER LINK** | Connection state, heartbeat health, link latency, and mission mode. |

### Severity levels

`NORMAL`, `WARNING`, `ERROR`, `OFFLINE`, and `NO DATA` are rendered through the Qt dynamic
property `severity` defined in `ui/styles/stylesheet.qss`. Severity is **always** carried by
text as well as colour — a critical battery reads `CRITICAL`, an offline LiDAR reads `OFFLINE`.

### Data flow

```
MockCommsClient (20 Hz, deterministic)
        │  telemetry_received(RoverState)
        ▼
TelemetryBridge ──► StateManager  (single source of truth, thread-safe, Qt-free)
        │                 │
        │                 │ get_snapshot()  (independent deep copy, staleness evaluated on read)
        │                 ▼
        └──────► EventBus.telemetry_updated   (throttled to ui_refresh_fps, default 30 FPS)
                          ▼
                     TelemetryPanel  →  Power / Motion / Attitude / Sensors / Rover Link cards
```

The publish rate and UI cap are independent, and both are configured in `config/app_config.yaml`:

```yaml
telemetry:
  mock_rate_hz: 20        # Mock telemetry publish rate (MOCK DATA ONLY)
  ui_refresh_fps: 30      # Max telemetry widget repaint rate
  stale_timeout_ms: 2000  # Telemetry older than this is shown as STALE
```

### Mock telemetry behaviour

`MockTelemetryGenerator` produces deterministic, bounded, time-varying values — no
uncontrolled randomness — so the interface looks realistic and behaves identically on
every run:

| Channel | Behaviour |
| :--- | :--- |
| Battery | Starts near full and drains linearly (~1.5 %/min) with a small load ripple. |
| Voltage | Derived from charge level across a 7S pack range (21.0–29.4 V), sagging under current draw. |
| Speed | Smooth sine profile in a plausible ~0.09–0.55 m/s band. |
| Wheel RPM | Computed from speed and mock wheel geometry, with a left/right split from the turn rate. |
| Roll / Pitch | Small bounded oscillations (two superimposed sine terms). |
| Yaw | Integrated slow turn rate, wrapped to 0–360°. |
| Latency | Baseline ~4.2 ms with a slow swell and bounded seeded jitter. |
| Sensors | Online, with brief deterministic `DEGRADED` windows for LiDAR (every 47 s) and encoders (every 83 s). |
| Diagnostics | Jetson temperature, CPU, GPU, and RAM as bounded oscillations. |

The generator is stepped by an explicit time delta rather than the wall clock, so tests can
advance it to any point in the profile without sleeping.

### Failure and fallback states

| Condition | UI behaviour |
| :--- | :--- |
| Never connected | Every card shows `--` / `NO DATA`; heartbeat reads `OFFLINE`. |
| Disconnected | Telemetry is invalidated — values are cleared rather than frozen at their last reading. |
| Stale (no packets for `stale_timeout_ms`) | Heartbeat reads `STALE` (warning); readings are withheld instead of being presented as live. |
| Mock mode | Link card reads `MOCK MODE`. |
| Malformed / missing snapshot | Ignored safely; the panel falls back to its no-data state without raising. |

---

## 5. Running Automated Tests

Run the full 249-test pytest suite:

```bash
pytest
```

Everything runs headless and without a rover: Qt widgets are exercised under
`QT_QPA_PLATFORM=offscreen`, and the ROS 2 message conversions are tested against plain
attribute stand-ins so they pass on a host with no ROS 2 installed.

```bash
QT_QPA_PLATFORM=offscreen pytest        # on a machine with no display
```

| Suite | Covers |
| :--- | :--- |
| `test_ros2_conversions.py` | Quaternion→Euler, odometry, joint states, battery, diagnostics, phase/mode parsing, heartbeat latency |
| `test_comms_backend.py` | Backend selection, new config, mode/E-Stop routing through `StateManager` |
| `test_control_widgets.py` | Mode/action buttons, E-Stop lockout, control status rendering |
| `test_jetson_terminal.py` | VT100 rendering, ANSI handling, key→PTY translation, SSH degradation |
| `test_main_window_controls.py` | Layout wiring and command round-trips end to end |
| `test_video_pip.py` | Picture-in-picture geometry, independence of the two streams |
| `test_terminal_concurrency.py` | Running commands while another is running; shell routing, busy marking, limits |

---

## 6. Further Documentation

| Document | Covers |
| :--- | :--- |
| [`docs/COMMANDS.md`](docs/COMMANDS.md) | **Every command, in order.** Setup, bring-up, the three laptop terminals, diagnostics, and the competition-day checklist. Start here. |
| [`docs/comms_architecture.md`](docs/comms_architecture.md) | **Rover ↔ MCC communications for the competition run.** The four-plane model, the 10-minute setup sequence, scenario-by-scenario behaviour, bandwidth budget, rulebook constraints, and how other Lunabotics teams approached it. |
| [`docs/architecture.md`](docs/architecture.md) | Mission Control's internal software architecture: threading model, state ownership, subsystem design. |
| [`docs/video_architecture.md`](docs/video_architecture.md) | Video transport analysis, Jetson GStreamer pipelines, streaming comparison matrix, latency measurement methodology. |
| [`src/mission_control/comms/ros2/README.md`](src/mission_control/comms/ros2/README.md) | The ROS 2 topic contract to agree with the rover-side team. |

---

## 7. Current Limitations

**Not yet verified against the physical rover.** The ROS 2 client has been tested end to
end over real DDS — link-up, every conversion, all four outbound commands, and the
watchdog — but against a stand-in node on the operator laptop, not against the Jetson. The
following remain open:

- **Mock is still the default**: with `comms.backend: auto` and `app.mock_mode: true`, every telemetry value comes from `MockTelemetryGenerator` and is not a measurement. Switch per [§4c](#4c-connecting-to-the-real-rover-ros-2).
- **Topic and message contract is assumed**: topic names are configurable, but the message *types* and the `/mission/cmd` action vocabulary (`EXCAVATE` / `DUMP` / `ONE CYCLE AUTO`) are what Mission Control expects. These must be agreed with whoever writes the rover-side nodes; nothing on the Jetson subscribes to them yet.
- **Joint and diagnostic names are matched heuristically**: wheel sides are identified by substrings (`left`/`right` + `wheel`/`drive`/`track`) and Jetson stats by diagnostic key names. Real hardware names should be confirmed and the hint lists in `conversions.py` narrowed.
- **Heartbeat latency needs synchronised clocks**: derived from `Header.stamp`, so it is only meaningful with NTP/PTP between laptop and Jetson. Unsynchronised clocks read `0.0 ms`.
- **No E-Stop acknowledgement**: the command is published and the rover's `/safety/estop` echo is believed, but there is no timeout that escalates if the rover never confirms. Commands are queued and drained at 10 Hz, so worst-case dispatch latency is ~100 ms.
- **SSH host keys use `AutoAddPolicy`**: appropriate for a private competition LAN and a Jetson that gets reflashed; not appropriate on an untrusted network.
- **Hardware Camera**: both streams default to `TestPatternVideoSource`. Live RTSP from the Jetson needs the `cameras.*.uri` fields filled in and has not been exercised against real hardware.
- **LiDAR / Map**: the map placeholder was removed to make room for the control column. Livox MID-360 pointcloud integration still needs a home in the layout.
- **Mission phase**: still operator-selected from the dev controls by default. The rover can now drive it via `/mission/state`, but there are still no transition rules and no state machine.
- **Excavation telemetry**: `ExcavationData` is now populated from `/joint_states` (drum RPM, arm extension) but is still not displayed on any card.
=======
# UI-Algosteam
>>>>>>> origin/main
