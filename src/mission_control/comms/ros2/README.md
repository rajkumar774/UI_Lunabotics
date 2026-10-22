# ROS 2 Humble Adapter

## Overview

This module provides the interface between the PySide6 Mission Control GUI and the ROS 2 Humble environment running on the rover's NVIDIA Jetson Orin Nano (Ubuntu 22.04).

**Status: implemented.** The adapter creates a node, subscribes, publishes, and runs a
heartbeat watchdog. It has been exercised end to end over real DDS against a stand-in
publisher, but **not yet against the physical Jetson**.

| File | Role |
| :--- | :--- |
| `client.py` | `ROS2CommsClient` — thin Qt-facing adapter implementing `BaseCommsClient`. |
| `worker.py` | `ROS2WorkerThread` — owns the rclpy context, node, subscriptions and publishers. |
| `conversions.py` | Pure ROS-message → `RoverState` mapping. No rclpy import; unit-testable without ROS 2. |

## Discovery

The operator laptop and the Jetson share a `ROS_DOMAIN_ID` (set in
`config/rover_config.yaml` under `rover.ros_domain_id`) and DDS discovers topics
automatically. There is no broker, and no IP address is configured for topic data — the
`network.rover_ip` setting is used only for the RTSP video URIs and the SSH terminal.

Mission Control exports `ROS_DOMAIN_ID` into the environment *before* initialising the
rclpy context, since the domain is read at init and cannot be changed afterwards.

## QoS

Subscriptions are `BEST_EFFORT`, publishers `RELIABLE` — the only pairing that cannot
silently fail to match in either direction. A QoS mismatch shows up as a topic that appears
in `ros2 topic list` but never delivers a message.

## Known rclpy gotcha

`diagnostic_msgs/DiagnosticStatus.level` is a ROS `byte`, which rclpy represents as a
one-byte Python `bytes` object: `DiagnosticStatus.WARN` is `b'\x01'`, not `1`. Parsing it
numerically raises and would quietly downgrade every warning and error to OK. See
`conversions.diagnostic_level()`.

## Architecture & Threading Model

```
+-------------------------------------------------------------------+
|                     PySide6 UI (Main Thread)                      |
|                 Listens to Qt Signals & Slots                     |
+-------------------------------------------------------------------+
                                ^
                                | Qt Signals (QueuedConnection)
                                v
+-------------------------------------------------------------------+
|                   ROS2WorkerThread (QThread)                      |
|  - Manages rclpy.executors.SingleThreadedExecutor / MultiThreaded |
|  - Dispatches ROS 2 subscription callbacks                        |
|  - Sends ROS 2 publisher messages                                 |
+-------------------------------------------------------------------+
                                ^
                                | DDS Network Transport
                                v
+-------------------------------------------------------------------+
|               NVIDIA Jetson Orin Nano (ROS 2 Humble)              |
+-------------------------------------------------------------------+
```

## Publishers (implemented)

| Topic Name | Message Type | Rate | Description |
| :--- | :--- | :--- | :--- |
| `/cmd_vel` | `geometry_msgs/msg/Twist` | On demand | Manual teleoperation drive setpoints. |
| `/safety/estop` | `std_msgs/msg/Bool` | Event | Emergency Stop engage / release. |
| `/mission/autonomy_mode` | `std_msgs/msg/String` | Event | Control mode request: one of the `MissionMode` values. |
| `/mission/cmd` | `std_msgs/msg/String` | Event | Discrete action: `EXCAVATE`, `DUMP`, `ONE CYCLE AUTO`. |

Commands are queued on the Qt thread and drained on the ROS thread at 10 Hz, so worst-case
dispatch latency is ~100 ms.

## Subscribers (implemented)

| Topic Name | Message Type | Expected Rate | Mapped to |
| :--- | :--- | :--- | :--- |
| `/rover/heartbeat` | `std_msgs/msg/Header` | 5 Hz | Link watchdog; latency from `stamp`. |
| `/imu/data` | `sensor_msgs/msg/Imu` | 50 Hz | Roll / pitch / yaw, degrees, yaw wrapped 0–360. |
| `/odom` | `nav_msgs/msg/Odometry` | 20 Hz | Ground speed and turn rate from `twist.twist`. |
| `/joint_states` | `sensor_msgs/msg/JointState` | 20 Hz | Wheel RPM per side; excavation drum and arm. |
| `/battery_state` | `sensor_msgs/msg/BatteryState` | 1–5 Hz | Charge, voltage, draw, cell temperature. |
| `/diagnostics` | `diagnostic_msgs/msg/DiagnosticArray` | 1 Hz | Jetson CPU/GPU/RAM/temp, sensor health, alerts. |
| `/mission/state` | `std_msgs/msg/String` | Event | Mission phase. |
| `/mission/autonomy_mode` | `std_msgs/msg/String` | Event | Reported control mode (rover is authoritative). |
| `/safety/estop` | `std_msgs/msg/Bool` | Event / 10 Hz | Latched E-Stop (rover is authoritative). |

### Joint naming

`/joint_states` entries are matched by substring, so the rover's actual joint names must
contain these hints:

| Field | Requires | And one of |
| :--- | :--- | :--- |
| Left wheel RPM | `wheel`, `drive` or `track` | `left`, `_l_` or `port` |
| Right wheel RPM | `wheel`, `drive` or `track` | `right`, `_r_` or `starboard` |
| Drum RPM | `drum`, `bucket`, `auger` or `excav` | — |
| Arm extension | `arm`, `linkage`, `boom` or `lift` | — |

Multiple joints per side are averaged. A side with no matching joint keeps its previous
reading rather than reporting a fabricated `0.0`. Velocities are rad/s, converted to RPM.

### Diagnostic naming

Status entries feed sensor health by substring on `name`: `imu`/`inertial` → IMU,
`encoder`/`odom`/`wheel` → encoders, `lidar`/`laser`/`scan` → LiDAR. Jetson stats are read
from `KeyValue` pairs whose key contains `cpu`, `gpu`, `ram`/`memory` or `temp`. An
unreported group keeps its previous status rather than defaulting to ONLINE.

## Not yet subscribed

| Topic Name | Message Type | Blocked on |
| :--- | :--- | :--- |
| `/livox/lidar` | `PointCloud2` / livox `CustomMsg` | The map view needs a home in the layout (see architecture §5.4). Never stream the raw cloud over the link -- send derived obstacles. |
| `/camera/image_raw/compressed` | `sensor_msgs/msg/CompressedImage` | Video ingests over RTSP instead; this is the documented fallback path. |

## Services & Actions (roadmap)

Mission Control currently dispatches modes and actions as plain `String` topics, which
needs no custom interface package on either side. Promoting them to services or actions
would give acknowledgements, rejection reasons and progress feedback — worth doing once
the rover-side nodes exist and a shared interface package is agreed.

| Service / Action Name | Type | Would add |
| :--- | :--- | :--- |
| `/mission/set_mode` | `std_srvs/srv/SetBool` or custom Srv | Accept/reject result for a mode change, instead of assuming it took. |
| `/navigate_to_pose` | `nav2_msgs/action/NavigateToPose` | Autonomous navigation goals with progress. |
| `/excavation/run_sequence` | Custom Action | Cycle progress and a cancel path for `ONE CYCLE AUTO`. |

## Contract to agree with the rover team

Topic *names* are configurable in `config/rover_config.yaml`. These are not, and must match:

1. The message **types** in the subscriber table above.
2. The `/mission/cmd` vocabulary: `EXCAVATE`, `DUMP`, `ONE CYCLE AUTO`.
3. The `/mission/autonomy_mode` vocabulary — `MissionMode` values, though
   `conversions.mission_mode_from_string()` also accepts common short spellings
   (`teleop`, `auto`, `dig`, `dump`, `standby`).
4. Joint and diagnostic naming, per the hint tables above.
5. NTP or PTP clock sync between laptop and Jetson, if heartbeat latency is to mean anything.
