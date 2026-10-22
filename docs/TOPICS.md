# Topic Requirements — what the Jetson must publish for the UI to light up

**Nothing in the UI was changed to produce this document.** It describes what the
code already expects, as of the current `main`.

This is the contract between the Jetson and Mission Control. If the Jetson
publishes these topics, with these types, carrying these fields, every card in
the UI fills in by itself. Nothing else needs to happen.

---

## 1. The one place to change a topic name

> **`config/rover_config.yaml`, under `ros_topics:` — that is the only place.**

Every topic name is read from that block at startup and handed to the ROS 2
worker. No topic name is hard-coded anywhere in the Python. If the Jetson
publishes `/agazhvan/battery` instead of `/battery_state`, you change one line:

```yaml
ros_topics:
  battery: "/agazhvan/battery"     # <- was /battery_state
```

Save, restart the UI. No code edit, no rebuild, no reinstall.

The full block as it ships:

```yaml
ros_topics:
  cmd_vel: "/cmd_vel"
  heartbeat: "/rover/heartbeat"
  imu: "/imu/data"
  odometry: "/odom"
  joint_states: "/joint_states"
  battery: "/battery_state"
  lidar: "/livox/lidar"
  lidar_imu: "/livox/imu"
  camera_image: "/camera/image_raw"
  camera_compressed: "/camera/image_raw/compressed"
  mission_state: "/mission/state"
  mission_cmd: "/mission/cmd"
  autonomy_mode: "/mission/autonomy_mode"
  diagnostics: "/diagnostics"
  estop: "/safety/estop"
  joy: "/joy"
  teleop_status: "/rover/teleop_status"
```

Renaming a topic here is always safe. **Changing a message *type* is not** —
the type is what the conversion code reads fields off, so a different type
needs a code change in `src/mission_control/comms/ros2/conversions.py`.

---

## 2. What the UI subscribes to (Jetson -> laptop)

Nine subscriptions. These are the ones that make the display move.

| Config key | Default topic | Message type | Fills in the UI |
|---|---|---|---|
| `imu` | `/witmotion/imu` | `sensor_msgs/Imu` | ATTITUDE card: roll, pitch, yaw |
| `odometry` | `/ekf_odom/odometry` | `nav_msgs/Odometry` | MOTION card: speed, turn rate |
| `joint_states` | `/wheel/encoders` | `sensor_msgs/JointState` | MOTION card: wheel RPM, L/R RPM; drum + arm |
| `battery` | `/battery_state` | `sensor_msgs/BatteryState` | POWER card: %, voltage, draw, state |
| `diagnostics` | `/diagnostics` | `diagnostic_msgs/DiagnosticArray` | SENSORS card + Jetson CPU/temp + alerts |
| `heartbeat` | `/rover/heartbeat` | `std_msgs/Header` | ROVER LINK: connection, heartbeat, latency |
| `mission_state` | `/mission/state` | `std_msgs/String` | Mission phase in state (no widget today) |
| `autonomy_mode` | `/mission/autonomy_mode` | `std_msgs/String` | CONTROL STATUS: CTRL MODE row |
| `estop` | `/safety/estop` | `std_msgs/Bool` | SAFETY STATUS badge, E-STOP row |

### What the UI publishes (laptop -> Jetson)

| Config key | Default topic | Message type | Sent when |
|---|---|---|---|
| `cmd_vel` | `/cmd_vel` | `geometry_msgs/Twist` | Teleop drive commands |
| `estop` | `/safety/estop` | `std_msgs/Bool` | E-STOP pressed / released |
| `mission_cmd` | `/mission/cmd` | `std_msgs/String` | An action bound with `mode: topic` |
| `autonomy_mode` | `/mission/autonomy_mode` | `std_msgs/String` | Operator sets a mode |

`/safety/estop` is both subscribed and published: the UI asserts it and also
reads back what the rover believes, so the two cannot silently disagree.

### Declared in config but NOT wired to anything

Be aware of these — publishing them changes nothing in the UI today:

| Key | Why it is inert |
|---|---|
| `lidar`, `lidar_imu` | The LIDAR row is driven by `/diagnostics`, not by the point cloud. The UI never subscribes to the Livox stream. |
| `camera_image`, `camera_compressed` | Video does **not** travel over ROS. It is GStreamer/RTSP straight into the player. |
| `joy` | Published by `joy_node` on *this laptop*, not by the UI, and not read by it. |
| `teleop_status` | Reserved for the planned TELEOP indicator. Not yet subscribed. |

---

## 3. Field-level requirements

Getting the topic name right is half of it. The UI reads specific fields, and
in three places it matches on *string content* inside the message.

### `/imu/data` — `sensor_msgs/Imu`
Only `orientation` (x, y, z, w) is read, converted to roll/pitch/yaw in degrees.
A zero quaternion reads as 0/0/0 rather than throwing. Angular velocity and
linear acceleration are ignored.

### `/odom` — `nav_msgs/Odometry`
Only `twist.twist.linear.x` (m/s, shown as SPEED) and `twist.twist.angular.z`
(rad/s, shown as TURN RATE). Pose is ignored. Per-wheel RPM does **not** come
from here — it is carried forward from `/joint_states`.

### `/joint_states` — `sensor_msgs/JointState`
Matched by **substring in `name[]`, case-insensitive**. `velocity[]` is taken as
**rad/s and converted to RPM** by the UI, so publish rad/s, not RPM.

A joint counts as a drive wheel if its name contains `wheel`, `drive` or
`track`, and is then assigned to a side:

- **Left** if the name contains `left`, `_l_` or `port`
- **Right** if the name contains `right`, `_r_` or `starboard`

Each side is averaged. A side with no matching joint keeps its previous reading
instead of displaying a fabricated `0`.

Also matched, from the same message:

- **Drum RPM** — name contains `drum`, `bucket`, `auger` or `excav`; read from `velocity[]`
- **Arm extension** — name contains `arm`, `linkage`, `boom` or `lift`; read from `position[]`, multiplied by 100 and clamped to 0–100%

Names like `front_left_wheel_joint` and `rear_right_wheel_joint` work as-is.

### `/battery_state` — `sensor_msgs/BatteryState`
- `percentage` — accepts **either** 0–1 or 0–100. A value in 0..1 is multiplied by 100; the result is clamped to 0–100.
- `current` — **sign is ignored**; the absolute value is shown as DRAW, so the ROS convention of negative-on-discharge is fine.
- `voltage`, `temperature` — used as published.

### `/diagnostics` — `diagnostic_msgs/DiagnosticArray`
This one topic drives three different parts of the UI.

**Sensor health rows** come from `status[].name`, matched by substring:

| UI row | Name must contain |
|---|---|
| IMU | `imu` or `inertial` |
| ENCODERS | `encoder`, `odom` or `wheel` |
| LIDAR | `lidar`, `laser` or `scan` |

and `status[].level` maps as `0 -> ONLINE`, `1 -> DEGRADED`, `2 -> OFFLINE`,
`3 -> OFFLINE`. A group that is not named in the array keeps its previous
status — it is never silently marked ONLINE.

**Jetson vitals** come from the `status[].values[]` KeyValue pairs, matched by
substring in the key, case-insensitive. The value may carry a `%` or `°C`
suffix; it is stripped before parsing.

| Shown as | Key contains (first match wins) |
|---|---|
| CPU | `cpu usage`, `cpu_usage`, `cpu load`, `cpu` |
| GPU | `gpu usage`, `gpu_usage`, `gpu load`, `gpu` |
| RAM | `ram usage`, `ram_usage`, `memory usage`, `mem usage`, `ram`, `memory` |
| Temperature | `temp`, `temperature`, `thermal` |

**Power logger readings.** `sensor_msgs/BatteryState` has no field for power or
cumulative energy, so the UI reads both from the KeyValues of a status whose
**name** contains `power`, `pzem` or `battery` — scoped that way so a CPU status
carrying a `power mode` key cannot be mistaken for pack draw:

| Shown as | Key contains | Units |
|---|---|---|
| POWER | `power_w`, `power` | watts |
| ENERGY | `energy_wh`, `energy` | watt-hours |

An absent key leaves the previous reading in place; it is never reset to 0,
because a real 0.0 W and "never reported" mean different things to an operator.

**Alerts**: any status at level >= 1 is listed as `"<name>: <message>"`.

A DiagnosticArray may report only what changed — absent fields keep their
previous value rather than resetting.

### `/rover/heartbeat` — `std_msgs/Header`
Drives ROVER LINK and the JETSON badge. The UI treats the rover as connected
while heartbeats keep arriving, and as lost after
`heartbeat_timeout_ms` (currently **1000 ms**, in `config/app_config.yaml`).

`stamp` is used for one-way latency. **This is only meaningful if both clocks
are synchronised** — a rover clock ahead of the laptop's reads as 0.0 ms rather
than a negative number. Publishing at **5–10 Hz** is the sensible range.

> Worth knowing: the Jetson clock was sitting at 1970 during the boot audit.
> Until that is fixed, treat the latency figure as meaningless; connection
> state itself is unaffected.

### `/mission/autonomy_mode` and `/mission/state` — `std_msgs/String`
Parsing is forgiving. Separators `-`, `_`, space and `/` are all equivalent and
case is ignored, so `"MANUAL TELEOP"`, `"manual_teleop"` and `"Manual-Teleop"`
are the same token.

**Control modes** (`/mission/autonomy_mode`) — canonical values:
`IDLE / STANDBY`, `MANUAL TELEOP`, `AUTONOMOUS NAV`, `EXCAVATION`,
`DUMP / CONSTRUCTION`, `EMERGENCY STOP`.

These short spellings are accepted too:

| Publish | Understood as |
|---|---|
| `TELEOP`, `MANUAL` | MANUAL TELEOP |
| `AUTO`, `AUTONOMOUS`, `NAV` | AUTONOMOUS NAV |
| `EXCAVATE`, `DIG` | EXCAVATION |
| `DUMP`, `CONSTRUCTION` | DUMP / CONSTRUCTION |
| `ESTOP`, `E_STOP` | EMERGENCY STOP |
| `IDLE`, `STANDBY` | IDLE / STANDBY |

**Mission phases** (`/mission/state`): `PRE-CHECK`, `TELEOP`, `AUTONOMOUS`,
`DUMP`, `POST-RUN`. Run-together spellings (`precheck`, `postrun`) are accepted.
An unrecognised string is ignored rather than clearing the current value.

### `/safety/estop` — `std_msgs/Bool`
`data: true` means **stopped**. Latches the SAFETY STATUS badge.

---

## 4. QoS — the thing that silently breaks

The UI subscribes **BEST_EFFORT** and publishes **RELIABLE**, deliberately:

- a BEST_EFFORT subscriber matches a publisher of *either* reliability
- a RELIABLE publisher matches a subscriber of *either* reliability

So whichever the Jetson chooses, it matches. This avoids the classic failure
where a topic appears in `ros2 topic list` and shows a publisher count, but no
message is ever delivered.

**The RMW must match on both machines.** This is the one setting that produces
total, silent invisibility — `ros2 topic list` shows nothing at all, with no
error. During the boot audit the Jetson's `~/.bashrc` set `cyclonedds` while
its systemd units set `fastrtps`, and the laptop defaults to `fastrtps`. Both
ends must agree, and both must share `ROS_DOMAIN_ID` (currently **17**).

---

## 5. Verifying, in order

Run these from the laptop. The first one that fails is the problem.

```bash
export ROS_DOMAIN_ID=17
echo $RMW_IMPLEMENTATION          # must be identical on the Jetson

ros2 topic list                   # nothing at all? -> RMW or domain mismatch
ros2 topic hz   /rover/heartbeat  # expect roughly 5-10 Hz
ros2 topic echo /battery_state    # does it carry the fields above?
ros2 topic echo /joint_states     # do the names contain wheel/left/right?
ros2 topic info /odom --verbose   # confirms the QoS actually in use
```

If a topic lists and has a publisher but `echo` prints nothing, that is a QoS
mismatch. If `ros2 topic list` is empty, it is RMW or `ROS_DOMAIN_ID`.

---

## 5b. Who starts these on the rover

The MOTION and ATTITUDE cards stay blank until `rover_localization` is
running, because it owns every node that feeds them:

| Topic | Published by | Default names/units |
|---|---|---|
| `/witmotion/imu` | `imu_modbus_node` | — |
| `/ekf_odom/odometry` | `robot_localization` `ekf_node` (fused) | — |
| `/wheel/encoders` | `wheel_odom_node` | joints `left_track`, `right_track`, velocity in rad/s |

`/ekf_odom/odometry` is the **fused** estimate, chosen over raw `/wheel/odom`
because skid steer makes the wheel-only pose untrustworthy — `wheel_odom_node`
itself sets a huge pose covariance for that reason. `/fastlio/odom_base` is the
other raw alternative.

**What starts it:**

| UI button | Launch file | Starts the sensor stack? |
|---|---|---|
| ONE CYCLE AUTO | `rover_navigation full_autonomy.launch.py` | **Yes** — includes `traverse` → `rover_localization` + `perception` + nav2 + teleop |
| EXCAVATE | `excavate_dump excavate_phase.launch.py` | **No** — `excavate_node` only |
| DUMP | `excavate_dump dump_phase.launch.py` | **No** — `dump_node` only |

So pressing EXCAVATE or DUMP on their own leaves MOTION and ATTITUDE blank.
They assume the stack is already up.

## 6. Minimum set to get the UI looking alive

If the Jetson can only publish a few things to start with, publish these three:

1. **`/rover/heartbeat`** (`std_msgs/Header`, 5–10 Hz) — turns ROVER LINK and the JETSON badge green. Everything else reads as offline until this arrives.
2. **`/battery_state`** — fills the POWER card, the most-watched card during a run.
3. **`/diagnostics`** — fills the SENSORS card and the Jetson CPU/temp line.

Then add `/odom` + `/joint_states` (MOTION) and `/imu/data` (ATTITUDE).
