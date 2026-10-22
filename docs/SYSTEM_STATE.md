# System State — as of 19 October 2026 (updated after first Team_1 run)

What is configured on both machines right now, why, and what is still open.
Written for the team and for future-you: every non-obvious choice here was made
to fix something that actually broke during setup.

---

## 0. What was done

Everything below was found by measuring on the real rover, not assumed. The
short version, in the order it was tackled:

| # | Area | Outcome |
|---|---|---|
| 1 | **DDS link** | Root cause was a **vendor mismatch** (laptop FastDDS, Jetson CycloneDDS) — they cannot see each other at all. Both now on CycloneDDS with unicast-only discovery. §2 |
| 2b | **Participant limit** | `full_autonomy` aborted ~27 nodes with SIGABRT; `MaxAutoParticipantIndex` was 20, now 80, and one config replaces two. §2 |
| 2 | **Environment** | Vars moved to `/etc/environment` (PAM reads it for every session); `rover-dds-check` added. §2 |
| 3 | **Jetson clock** | Was at **1970**. Set from the laptop and written to RTC. §7 |
| 4 | **Boot services** | `rover-teleop`, `rover-power`, `rover-video` all enabled. Two latent bugs fixed in the teleop unit. §3 |
| 5 | **Address-rebind trap** | Services looked `active` while every send failed — CycloneDDS had bound the old address. NetworkManager dispatcher added. §3 |
| 6 | **Teleop QoS** | `/joy` and `/cmd_vel` now BEST_EFFORT, depth 1. §3 |
| 7b | **Sensors on boot** | `rover-sensors.service` runs the WIT IMU + wheel encoders from power-on, so ATTITUDE/MOTION do not wait for ONE CYCLE AUTO. §3 |
| 7 | **Cameras** | Rear OV9281 shared with AprilTag via a ROS topic instead of fighting over `/dev/video0`; front at 640x360@30. The rear driver autostarts; the **front D456 is launched by hand**. §3b |
| 8 | **Power logger** | New `rover_power` package publishing `/battery_state` from the PZEM-003. §4 |
| 9 | **UI launch** | `run_ui.sh` added — the UI had been silently running in **MOCK MODE**. §5 |
| 10 | **UI theme** | Burgundy, with the E-Stop and severity ladder kept legible. §6 |
| 11 | **UI bugs** | Fullscreen froze the app; STOP STREAM restarted the stream instead of stopping it. Both fixed, both with regression tests. §6 |
| 12 | **Docs** | [TOPICS.md](TOPICS.md) (the Jetson↔UI topic contract) and a tech-stack page in `How_It_Works.pdf`. |

Test suite: **364 passing**.

---

## 1. The two machines

| | MCC laptop | Jetson Orin Nano |
|---|---|---|
| User | `agahvan20` | `agazhvan2` (password in the team password manager) |
| On Team_1 | `192.168.2.50` (static) | `192.168.2.20` (static) |
| On campus WiFi | DHCP, changes constantly | DHCP, changes constantly |
| ROS | Humble | Humble |
| Wired | — | `enP8p1s0` = `192.168.1.40` (Livox MID-360) |

---

## 2. DDS — the part that took longest

### The root cause of "DDS not working"

**The two machines were running different DDS vendors.** The laptop had only
FastDDS installed; the Jetson's `~/.bashrc` forced CycloneDDS. These cannot
interoperate — not unreliably, but *at all*, silently, with `ros2 topic list`
simply returning nothing.

This was never a network problem. Hours were spent on discovery and QoS before
the vendor mismatch was found, so check this **first** if the link ever dies.

### What is configured now

Both machines run **CycloneDDS**, `ROS_DOMAIN_ID=17`, with **unicast-only**
discovery. Campus WiFi (and most arena APs) drop multicast, which makes the
default SPDP discovery fail silently.

Config files — same content, different interface name and path:

| Machine | File | Interface |
|---|---|---|
| Laptop | `~/.ros/cyclonedds.xml` | `wlp4s0` |
| Jetson | `/etc/rover/cyclonedds.xml` | `wlP1p1s0` |

Both set `<AllowMulticast>false</AllowMulticast>` and list the same four peers:

```xml
<Peer address="localhost"/>
<Peer address="172.17.6.30"/>   <!-- laptop, campus  -->
<Peer address="172.17.8.99"/>   <!-- Jetson, campus  -->
<Peer address="192.168.2.50"/>  <!-- laptop, Team_1  -->
<Peer address="192.168.2.20"/>  <!-- Jetson, Team_1  -->
```

Both networks are listed so the file needs no edit when switching. An
unreachable peer costs only a periodic unicast probe.

### Environment variables

Three variables must be set, and all three were moved to **`/etc/environment`**
on the Jetson, because PAM reads it for *every* session — interactive, login,
and non-interactive `ssh host cmd`. `.bashrc` covers only the first of those,
which is why services and SSH commands kept starting without them.

```
ROS_DOMAIN_ID=17
RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
CYCLONEDDS_URI=file:///etc/rover/cyclonedds.xml     # laptop: ~/.ros/cyclonedds.xml
```

**systemd does NOT read `/etc/environment`**, so every unit file repeats them
as explicit `Environment=` lines.

### Participant slots — what crashed full autonomy

Launching `full_autonomy` killed ~27 nodes at once, every one with **exit code
-6 (SIGABRT)**, within a millisecond of starting: livox, realsense, both
apriltag nodes, the EKFs, velocity_smoother, waypoint_follower.

**Cause: DDS participant slots ran out.** With unicast-only discovery and
`ParticipantIndex: auto`, every ROS node claims one numbered slot bounded by
`MaxAutoParticipantIndex`. It was **20**, against ~23 always-on nodes plus ~27
more in full autonomy. Exceeding it does not degrade gracefully — the node
**aborts**:

```
failed to initialize rcl node: rcl node's rmw handle is invalid
```

Proven, not guessed: forcing the limit to 0 on one healthy node reproduced the
identical abort (exit 134 = 128+6) while the normal limit ran fine.

The tell was already there — `cyclonedds_nav.xml` had been raised to **60**
while the system config stayed at 20. Someone hit this and patched the one
file in front of them.

**Fixed three ways:**

1. `MaxAutoParticipantIndex` is now **80** on both machines (~50 needed, so
   headroom). Not larger: unicast probe cost is `peers x this number`.
2. `~/cyclonedds_nav.xml` is now a **symlink** to `/etc/rover/cyclonedds.xml`.
   Two configs that must agree, but could drift, is what caused this; now there
   is one file.
3. The peer list dropped the two stale campus addresses. They were unreachable
   from Team_1 and each one multiplied probe traffic by the participant range
   for nothing — so there is more headroom AND less traffic than before.

> This is a cost of unicast-only discovery, which we chose because arena APs
> drop multicast. Multicast has no participant-index limit. If nodes ever abort
> at startup again, this number is the first thing to check.

### One DDS vendor, everywhere

`rover-video` was still on **FastDDS** while everything else — including the
camera driver feeding its stream bridge — was on CycloneDDS. Two vendors cannot
see each other; that mismatch cost a full day at the start of this project.
Every process on both machines is now `rmw_cyclonedds_cpp`, verified by reading
`/proc/<pid>/environ` rather than trusting the unit files.

**Why CycloneDDS and not FastDDS:** FastDDS has the same class of limit with a
*lower* default (`maxInitialPeersRange` = 4 vs 20), so switching would relocate
the problem into a config format that is not tuned, on both machines, with no
benefit. FastDDS does have shared-memory transport for same-machine traffic,
which would genuinely help ~27 local nodes moving images — that is a deliberate
performance project, not a crash fix.

### `rover-dds-check`

One command on either machine, reports in four lines whether the current shell
can talk DDS and names the specific fault:

```
host           : ubuntu (172.17.8.99)
ROS_DOMAIN_ID  : 17
RMW            : rmw_cyclonedds_cpp
CYCLONEDDS_URI : file:///etc/rover/cyclonedds.xml
environment OK
```

Every DDS outage during setup was one of those lines being wrong.

> **A shell only reads `.bashrc` when it starts.** Re-running a command in a
> terminal that was already open keeps the old environment. After any env
> change, open a *new* terminal — and run `ros2 daemon stop`, because the
> daemon caches the old environment too and will show a stale, empty graph.

---

## 3. Jetson services

| Service | Enabled | What it does |
|---|---|---|
| `rover-teleop` | **yes** | `ros2 launch rover_teleop joy_teleop.launch.py` from `lunar_ws` |
| `rover-power` | **yes** | PZEM-003 → `/battery_state` |
| `rover-net` | no | Superseded by the NetworkManager Team_1 profile |
| `rover-video` | **yes** | RTSP bridges: front :8554/stream, rear :8555/rear |

### `rover-teleop.service`

Two latent bugs were found here that would have failed silently at competition:

1. It set `RMW_IMPLEMENTATION=rmw_fastrtps_cpp`. Enabled as-is, boot teleop
   would have come up invisible to the laptop.
2. It launched `rover_serial.launch.py`, which **does not exist** in
   `lunar_ws`. It would have crash-looped forever.

Both fixed. The unit also has `Restart=always`, `CPUSchedulingPolicy=fifo`
priority 80 (verified applied), and `Wants=rover-net` removed — that unit exits
1 on an empty `WIFI_SSID` and would restart-loop.

`/etc/rover/rover.conf` now points `ROS_WS_TELEOP` at `lunar_ws`, not the stale
`rover_boot_ws`.

### The address-rebind trap (NetworkManager dispatcher)

**CycloneDDS binds its sockets to a concrete source address at node startup and
never rebinds.** On the first Team_1 boot this produced a service that looked
perfectly healthy — `enabled`, `active`, no crash — while every send failed:

```
ddsi_udp_conn_write to udp/192.168.2.50:11662 failed with retcode -1
ss -unap  ->  UNCONN  172.17.8.99:43568  joy_node     <-- the OLD campus address
```

NetworkManager had associated with `SECE_WIFI` first at boot, the services
started and bound `172.17.8.99`, then the higher-priority Team_1 profile took
over and the address became `192.168.2.20`, stranding every socket.

Fixed with `/etc/NetworkManager/dispatcher.d/90-rover-restart`, which restarts
`rover-teleop` and `rover-power` on `up` / `dhcp4-change` for `wlP1p1s0`.

> This failure mode is nastier than a crash: `systemctl status` says `active`
> and the journal shows no error until you look for `retcode -1`. If topics are
> missing but the services look fine, check what address the sockets bound:
> `sudo ss -unap | grep joy_node`.

### Teleop QoS

`joy_serial_node.py` subscribes to `/joy` and `/cmd_vel` with:

```python
TELEOP_QOS = QoSProfile(
    reliability=ReliabilityPolicy.BEST_EFFORT,
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
)
```

Over WiFi a retransmitted *stale* command is worse than a dropped one — a queue
of 10 would steer the rover from the past. A BEST_EFFORT subscriber still
matches RELIABLE publishers, so `joy_node` and Nav2 need no change.

> `joy_node` runs on **both** machines by choice, so `/joy` has two publishers.
> The Jetson's publishes nothing with no pad attached. If a gamepad is ever
> plugged into the Jetson it will fight the laptop's stick for control.

> **Never `pkill` a launch file** — it orphans the child nodes. Doing so left a
> second `joy_serial_node` alive on the old QoS, both driving the ESP32.
> `systemctl stop` kills the whole cgroup.

---

## 3b. Cameras

Two RTSP streams, both served by `stream_fixed.py` (one instance each):

| Stream | Port / mount | Source topic | Encoding |
|---|---|---|---|
| Front D456 | `:8554/stream` | `/camera/camera/color/image_raw` | rgb8, 640x360 @30 |
| Rear OV9281 | `:8555/rear` | `/rear_camera/image_raw` | mono8, 1280x720 -> scaled 640x360 |

### Frame rates, and why 60 fps is not reachable

**This Orin Nano module has no hardware video encoder** (NVENC is on Orin NX
and AGX, not Nano) -- `gst-inspect` finds no `nvv4l2h264enc`. Both streams are
encoded by `x264enc` on the CPU, and encode cost is what sets the ceiling.

Measured on the rover:

| | Result |
|---|---|
| OV9281 "60 fps" mode | **28 fps actual** -- the mode lies |
| OV9281 "120 fps" mode | **106 fps** (GStreamer) / 43 fps (OpenCV capture+decode) |
| `/rear_camera/image_raw` topic | 43 fps with `device_fps: 120` (was 5.5 at `60`) |
| Delivered to the UI | ~23 fps front, ~24 fps rear alone, ~10 fps rear with both running |

So `device_fps` is pinned to **120**, not 60: asking for 60 gets 28. The rear
is downscaled to 640x360 *before* the encoder (the ROS topic stays 1280x720 for
AprilTag's calibration), because halving each dimension quarters encode cost.

A genuine 60 fps rear view would mean not transcoding at all -- serving the
camera's native MJPG straight through, which costs no encoding. That needs
`ov9281_node` to publish `CompressedImage` alongside `mono8` and a small MJPEG
server to subscribe. Not done.

### External viewers starve the in-UI player

`video_launch.enabled` is **false**, deliberately. Those `gst-launch` viewers
(`~/frontcamera.txt`, `~/backcamera.txt`) connect to the same RTSP media as the
UI's own player, and the server serves both from one `shared=True` pipeline.
Measured: with a viewer running the UI player received **1 frame in 10 s**;
with it off, **241 frames (23.5 fps)**. They predate the UI being able to show
the streams itself.

### Sharing the rear camera with AprilTag

The rear OV9281 is reserved for `rover_localization` / AprilTag, and
`~/.config/rover/rear_camera_localization_only` disables the old direct-TCP
streamer (`stream_openaicam.py`, port 5000). **That marker stays in place.**

Instead the camera is published **once** as a ROS topic by `ov9281_node`
(`rear_camera.launch.py`), and both AprilTag and the RTSP bridge subscribe to
it. The driver **is** supervised by `rover-video`, so the rear view is up as
soon as the rover boots. Two subscribers to one topic cost nothing, whereas two processes opening
`/dev/video0` cannot both succeed. `stream_openaicam.py` is no longer
supervised at all — with the marker in place it exited immediately and the
supervisor restarted it every 2 seconds, forever.

`stream_fixed.py` needed one change for this: it accepted only `rgb8`/`bgr8`
and asserted `step == width * 3`, but the OV9281 is monochrome and publishes
`mono8`. It now carries a `BYTES_PER_PIXEL` table and maps `mono8 -> GRAY8`;
the pipeline's existing `videoconvert` handles GRAY8 to I420 for x264enc.

### The front D456 is launched by hand, the rear is not

`rover-video` runs only the RTSP **bridges**, which are subscribers. The front
D456 driver is started by `rover_bringup camera.launch.py` — the UI's CAMERAS
button — and **`perception.launch.py` and `tag_test.launch.py` both include
that same file**. Autostarting the D456 in `rover-video` would therefore
double-launch it the moment autonomy or AprilTag is run, so it is deliberately
left to the bringup. **The front stream shows nothing until the camera driver
is running.**

> The UI's BRINGUP button runs `ros2 launch rover_bringup rover.launch.py`, and
> **that launch file does not exist** — only `camera.launch.py` and
> `perception.launch.py` do. The button is broken.

## 4. Power logger (PZEM-003)

New package `rover_power` in `lunar_ws/src/drivers/`, publishing
`sensor_msgs/BatteryState` on **`/battery_state`** at 2 Hz — the topic the UI's
POWER card already subscribes to. It also emits a `power:` entry on
`/diagnostics`.

The POWER card shows **BATTERY, VOLTAGE, DRAW, POWER, ENERGY** and the derived
state. Watts and watt-hours cannot travel on `BatteryState` — it has no field
for either — so they ride the `/diagnostics` KeyValues and are matched only
against the logger's own status, never a CPU status that happens to carry a
`power mode` key. Live from the rover: `24.6 V / 0.8 A / 18.9 W / 220 Wh`.

**Device:** PZEM-003 on the FT4232H quad UART, Modbus-RTU, slave 1, 9600 8N2,
input registers (function code `0x04`).

A udev rule gives it a stable name, because all four `ttyUSB` ports are the
same chip and are numbered in enumeration order — `ttyUSB3` moves between boots:

```
/dev/rover_pzem -> ttyUSB3      (99-rover-pzem.rules, matches ID_USB_INTERFACE_NUM=03)
```

### Thresholds

| Parameter | Value | Meaning |
|---|---|---|
| `full_voltage` | 25.0 V | 100% |
| `empty_voltage` | 21.0 V | 0% |
| `warn_voltage` | 22.8 V | `/diagnostics` **WARN** |
| `floor_voltage` | 22.0 V | `/diagnostics` **ERROR** |

The gauge and the safety limits are **separate parameters on purpose**. Folding
the floor into the gauge would mean either lying about the charge or losing the
limit. The node logs a loud error if `warn_voltage` is ever set at or below
`floor_voltage`, because a silently unreachable low-battery warning is how a
pack gets flattened.

> `floor_voltage = 22.0 V` is an assumption, not a stated requirement. The
> original guidance was "must not go under 23 V"; the later instruction was
> "warn at 22.8 V". A WARN cannot sit below the ERROR, so the floor was moved
> down to keep the ladder ordered. **Confirm this number.**

### Design note

The node publishes **only on a successful read**. A failed read publishes
nothing, rather than a message full of zeros — a 0 V BatteryState is
indistinguishable from a flat pack on the operator's screen, and the UI already
renders absent telemetry as `NO_DATA`.

The PZEM has no temperature sensor, so `temperature` is `NaN`. The UI's
converter maps NaN to `0.0`; the POWER card has no temperature row today, so
nothing displays wrong.

---

## 5. Running the UI

**Use `./run_ui.sh`.** Running `PYTHONPATH=src python3 -m mission_control.main`
by hand is what puts the UI into **MOCK MODE**: it clobbers the PYTHONPATH that
ROS set, `rclpy` vanishes, and the app shows invented telemetry while looking
entirely normal. The giveaway is a CONNECT MOCK button in the header and every
card reading `--`.

Three things in the script are load-bearing, each of which silently broke the
UI during setup:

1. **`PYTHONPATH` is appended, not replaced.** `PYTHONPATH=src` clobbers what
   ROS's `setup.bash` set, `rclpy` vanishes, and the app quietly falls back to
   **MOCK MODE** while looking completely normal.
2. **`QT_QPA_PLATFORM=xcb`.** Under native Wayland, Qt's GTK platform theme
   asks GNOME for a settings key that does not exist and GLib aborts the
   process before a window appears.
3. **`set -u` must stay off** around `source /opt/ros/humble/setup.bash` — ROS
   setup scripts reference unset variables.

The script hard-fails if `rclpy` is not importable, rather than letting the app
start in mock mode.

```
ROS_DOMAIN_ID=17  RMW=rmw_cyclonedds_cpp
rclpy OK -- starting Mission Control (real ROS 2 backend)
[INFO] ROS 2 node 'mission_control_station' up on domain 17; subscriptions active.
```

---

## 6. UI changes

**Burgundy theme** applied across `stylesheet.qss` (118 colours) plus the
Python painter colours in `player_widget.py`, `telemetry_primitives.py` and
`terminal_view.py`.

Two safety-driven decisions:

- **E-Stop** was pushed to a vivid `#f01f35` with a 2 px bright border. Burgundy
  *is* dark red, so the emergency control risked blending into the chrome; the
  chrome is kept deliberately dark and desaturated so it stands out.
- **Severity ladder.** The rose accent `#e0657f` initially sat almost on top of
  the ERROR red `#f87171`, making a healthy battery indistinguishable from a
  failing one. Nominal readouts are now neutral warm-white and ERROR moved to
  `#ff5f5f`: **white → amber → red**. Burgundy is confined to chrome, which
  carries no state.

Terminal ANSI colours were deliberately **not** themed — a program emitting
cyan should render cyan.

### STOP STREAM did not stop (fixed)

The toggle branched on `self._stats.state`, but the button's label is written
by `handle_state_changed()` -- two different signals. A stream that went LIVE
before any stats update left the button reading STOP STREAM while the toggle
still believed it was stopped, so pressing it **restarted** the stream and the
label never returned to START STREAM. The toggle now reads the same
authoritative state the label does.

### Mission Control publishes NOTHING to the rover

The four publishers — `/cmd_vel`, `/safety/estop`, `/mission/cmd`,
`/mission/autonomy_mode` — were removed. The station is **subscribe-only**:
nothing it does can write to the rover's DDS graph. Commands leave by other
routes (the SSH session for mission actions, the laptop's own `joy_node` for
driving).

`_publish_command()` **raises** rather than dropping a command quietly, so the
caller reports it as a FAILED command in CONTROL STATUS. A command that
vanishes without a trace is worse than one reported as refused.

### The E-Stop button is present but NOT WIRED

It is back in the sidebar and carries the caption **"NOT WIRED — does not reach
the rover yet"**. Three deliberate choices, because this is a safety control
with nothing behind it:

- a press **still attempts the send**, so the refusal is real and lands in
  CONTROL STATUS — it is not faked;
- it **does not latch locally**. ENGAGED only ever reflects what the rover
  reports on `/safety/estop`. Latching on the press would show a stop that
  never happened, which is the worst possible lie on this button;
- the header E-Stop and SAFETY STATUS badge stay removed — one control is
  clearer than two that can disagree.

### Removed from the operator surface

ROVER LINK header badge, SAFETY STATUS badge, CONNECT MOCK, the ROVER LINK
card, and the SYSTEMS LOCALIZATION button. What survived each removal is the
*behaviour* behind it: a dropped link still blanks every card to NO DATA, and
a rover-reported E-Stop still locks out the action buttons.

### LOC indicator (stream footer)

Green while `/tag/rover_pose` fixes arrive, red when they stop, **grey when
there is no telemetry at all** — unknown, not a confident "not localized".
10-second window, because tag fixes are intermittent by nature and a short one
would flicker while localization was working fine.

### Fullscreen freeze (fixed)

Clicking FULLSCREEN froze the whole UI. `toggle_fullscreen()` called
`setWindowFlags(Qt.Window)` while the player was still inside the main
`QSplitter`, so the splitter kept geometry-managing a widget that had become a
top-level window — the two relayout each other in a loop.

The widget now leaves its parent **before** the flags change, and is
re-inserted at the same splitter index with the previous sizes on the way back.
`show()` is required after `setWindowFlags()`, which hides the widget.

> The pre-existing test passed throughout, because it built a *parentless*
> player — and a widget with no layout has nothing to fight with. Two
> regression tests now reproduce the real splitter layout; both fail against
> the old implementation.

Also added: [TOPICS.md](TOPICS.md) (the Jetson↔UI topic contract) and a tech
stack page in `How_It_Works.pdf`.

---

## 6b. If the arena SSID is not Team_1

The guidebook allocates `Team_###`, and you have been told `Team_1`. If that
changes on the day, **two commands** cover it — one per machine. Nothing else.

```bash
# On the Jetson
sudo nmcli con modify Team_1 802-11-wireless.ssid "Team_7" wifi-sec.psk "<new-password>"
sudo nmcli con up Team_1

# On the laptop
sudo nmcli con modify Team_1 802-11-wireless.ssid "Team_7" wifi-sec.psk "<new-password>"
sudo nmcli con up Team_1
```

(The profile *name* can stay `Team_1`; only the `ssid` field has to match the
air. Rename it too if the mismatch would confuse you under pressure.)

### Why nothing else changes

**The SSID appears nowhere except those two NetworkManager profiles** — not in
the repo, not in either `cyclonedds.xml`, not in any unit file. Everything else
is keyed on IP addresses, and **those addresses are ours, not the router's**:

| Depends on | Changes with the SSID? |
|---|---|
| NM profile (Jetson, laptop) | **Yes** — the two commands above |
| `/etc/rover/cyclonedds.xml`, `~/.ros/cyclonedds.xml` | No — peers are IPs |
| `config/rover_config.yaml` (`rover_ip`, `ssh_host`, camera URIs) | No |
| `/etc/rover/rover.conf` (`ROVER_IP`) | No |
| NM dispatcher, every `rover-*.service` | No — interface names |

Both machines are **static** (`.20` and `.50`), so they keep talking even if the
arena router hands out a completely different subnet: they are on the same
layer-2 network, and two statically configured hosts in `192.168.2.0/24` reach
each other directly. You lose internet through that router, which the rover
does not need.

> The one thing that would genuinely break this is being forced onto DHCP. Then
> the addresses move, the unicast peer lists go stale, and you are back to the
> campus-WiFi failure in section 7. Keep both ends static.

---

## 6c. Where to debug

Work down this list; each step rules out the one below it.

**0. Did a node ABORT at startup (exit -6 / SIGABRT)?**
```bash
grep "rmw handle is invalid" ~/.ros/log/*/launch.log
```
That is participant slots, not the network. Check `MaxAutoParticipantIndex` in
`/etc/rover/cyclonedds.xml` — see section 2.

**1. Is my shell able to talk DDS at all?**
```bash
rover-dds-check                 # either machine
```
Wrong RMW, wrong domain or an unreadable URI is the single most common cause.
**A shell reads this at START** — after any change, open a NEW terminal.

**2. Are both machines where I think they are?**
```bash
ip -br addr                     # expect 192.168.2.20 / .50
ping 192.168.2.20
```

**3. Is it just a stale daemon?**
```bash
ros2 daemon stop                # then retry; it caches the old graph AND env
```

**4. Services look `active` but no topics — what address did they bind?**
```bash
sudo ss -unap | grep -E 'joy_node|pzem_node'
```
CycloneDDS binds at process start and never rebinds. A node that came up before
the network settled is stranded on the old address while `systemctl status`
still says `active`. This is the trap in section 3.

**5. Were the running nodes started before the last env change?**
```bash
tr '\0' '\n' < /proc/<pid>/environ | grep -E 'RMW|CYCLONEDDS'
```

**6. Which topics actually exist, and at what rate?**
```bash
ros2 topic list
ros2 topic hz /witmotion/imu        # ATTITUDE card
ros2 topic hz /wheel/encoders       # MOTION wheel RPM
ros2 topic hz /battery_state        # POWER card
ros2 topic hz /tag/rover_pose       # the LOC indicator
```

**7. UI shows nothing / shows fake data**
```bash
./run_ui.sh                     # NEVER `PYTHONPATH=src python3 -m mission_control.main`
```
A CONNECT MOCK-looking UI with every card at `--` means MOCK MODE: the manual
command clobbers ROS's PYTHONPATH, `rclpy` vanishes, and the app invents
telemetry. `run_ui.sh` hard-fails instead.

**8. Streams blank**
```bash
sudo ss -tlnp | grep -E ':8554|:8555'      # is anything serving?
ros2 launch rover_bringup camera.launch.py # front D456 is MANUAL
```

### Card-by-card: what feeds what

| Card | Topic | Blank when |
|---|---|---|
| POWER | `/battery_state` + `/diagnostics` | `rover-power` down |
| ATTITUDE | `/witmotion/imu` | `rover-sensors` down |
| MOTION | `/wheel/odom`, `/wheel/encoders` | encoder ESP silent |
| SENSORS | `/diagnostics` + message liveness | see below |
| LOC (stream footer) | `/tag/rover_pose` | localization not running |

**SENSORS is the one that confuses.** It reports *health*, not data. IMU shows
ONLINE from message liveness because nothing publishes an IMU diagnostic;
ENCODERS shows whatever `wheel_odom_node` reports, and an explicit diagnostic
always beats liveness — so a rover-reported DEGRADED is never papered over by
traffic still arriving.

---

## 7. Known issues

**Campus WiFi (`SECE_WIFI`) is effectively unusable for this system.**
Observed during setup:

- DHCP reassigns addresses mid-session — the laptop moved `172.17.6.30` →
  `172.17.22.32` → back again within minutes.
- It scatters clients across *different* `/20` subnets, and the gateway does
  not pass traffic between them. When the Jetson was on `172.17.19.238` and the
  laptop on `172.17.6.30`, there was no ping, no SSH and no DDS.
- **Unicast peer lists cannot track this**, because the addresses change under
  them. This is the trade-off of disabling multicast.

On **Team_1 this disappears entirely** — both ends static, same subnet, already
in the peer lists. Team_1 has no internet, which is why campus WiFi is still in
use for development.

Other open items:

| Item | Status |
|---|---|
| Reboot test of the boot services | **Done** — both autostarted on the Team_1 boot, but see the address-rebind trap above |
| Laptop Team_1 profile | Done — laptop holds `192.168.2.50` |
| Laptop `/etc/environment` | Still needs sudo; `.bashrc` covers interactive shells only |
| Telemetry topics | Jetson publishes only `/joy`, `/cmd_vel`, `/control_mode`, `/battery_state`. No `/rover/heartbeat`, `/imu/data`, `/odom`, `/joint_states` — so most UI cards still read `--`. See [TOPICS.md](TOPICS.md) |
| `rover_config.yaml` `ssh_host` | `192.168.2.20` — correct on Team_1; wrong on campus WiFi |
| `rover-teleop` shows `disabled` but runs | The NM dispatcher `systemctl restart`s it, which starts a unit regardless of enabled state. `mask` it if you genuinely want it off |
| Encoder ESP silent | `wheel_odom_node` is connected to ttyACM0 but receives nothing; the rover reports `ESP32-2 encoders: no data received yet`. Power the motor/encoder electronics |
| E-Stop button | Present but NOT WIRED — see 6 |
| Front D456 driver | Launched by hand on purpose — see 3b. `:8554` stays blank until `camera.launch.py` is run |
| UI BRINGUP button | Broken: `rover_bringup rover.launch.py` does not exist |
| Rear camera at 60 fps | Not reachable — no hardware encoder on this module. Would need MJPG passthrough instead of H.264 transcoding. See 3b |
| PiP label | Reads **"FRONT CAM"** but shows the **rear** OV9281. Cosmetic, not renamed |
| External viewers | `video_launch.enabled: false` — enabling it starves the in-UI player. See 3b |
| TELEOP indicator | Needs `/rover/teleop_status`, not yet published |
| Jetson NTP | Clock was at 1970; set from the laptop and written to RTC, but campus blocks NTP so it will not self-correct |

---

## 8. Quick reference

```bash
# Is my shell able to talk DDS?          (either machine)
rover-dds-check

# Start the UI                            (laptop, from the repo)
./run_ui.sh

# After ANY environment change
ros2 daemon stop        # and open a new terminal

# Jetson services
sudo systemctl status rover-teleop rover-power
sudo journalctl -u rover-power -f

# Re-probe the PZEM after rewiring        (Jetson)
python3 ~/rover_tools/scan.py

# Services look active but no topics? Check what address they bound.
sudo ss -unap | grep -E 'joy_node|pzem_node'

# Change a battery threshold without editing code
ros2 launch rover_power pzem.launch.py floor_voltage:=22.5 warn_voltage:=23.0

# Front D456 -- launched by hand; :8554 is blank without it        (Jetson)
ros2 launch rover_bringup camera.launch.py
# (the rear OV9281 driver autostarts under rover-video)

# Are the streams being served?                                (Jetson)
sudo ss -tlnp | grep -E ':8554|:8555'

# Pull a frame from either stream                              (laptop)
ros2 topic hz /rear_camera/image_raw
```

### If the link dies, check in this order

1. `rover-dds-check` on **both** machines — same RMW? same domain? URI readable?
2. Are both machines on the **same subnet**? (`ip -br addr`)
3. `ros2 daemon stop`, then retry — stale daemon caches are not real outages.
4. Were the running nodes started **before** the last env change? Check with
   `tr '\0' '\n' < /proc/<pid>/environ | grep RMW`.
5. Only then suspect the network.
