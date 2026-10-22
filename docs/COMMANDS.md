# Commands Reference

Everything you need to type, in the order you need it. Keep this open during
setup.

**Network**

| Host | IP | Role |
| :--- | :--- | :--- |
| Jetson Orin Nano | `192.168.2.20` | rover compute, user `agazhvan2` |
| This laptop (UI) | `192.168.2.50` | Mission Control |
| Second laptop | `192.168.2.70` | CP Plus camera web UI only |

`ROS_DOMAIN_ID=17` on **both** the Jetson and this laptop. A mismatch means
the laptop silently never sees the rover — no error, topics just never appear.
It is the single most common cause of "nothing works".

---

## 0. One-time setup

Do these once. They stay done.

### 0.1 Passwordless SSH to the Jetson

Without this, auto-connect retries a password every 5 seconds, and some sshd
configs will lock you out mid-setup.

```bash
ssh-keygen -t ed25519 -C "mission-control" -f ~/.ssh/id_ed25519 -N ""
ssh-copy-id -i ~/.ssh/id_ed25519.pub agazhvan2@192.168.2.20   # password: once, here
ssh agazhvan2@192.168.2.20 'echo OK'                           # must NOT prompt
```

Then set in `config/rover_config.yaml`:
```yaml
jetson_ssh:
  key_path: "~/.ssh/id_ed25519"
```

### 0.2 Laptop static IP

```bash
ip -4 addr show | grep 192.168.2        # expect 192.168.2.50
```

### 0.3 UI backend

`config/app_config.yaml` — already set:
```yaml
app:
  mock_mode: false
comms:
  backend: "ros2"
```
Flip both back (`true` / `"mock"`) to demo the UI with no rover attached.

---

## 1. Jetson — start the rover

SSH in, or use the terminal panel inside the UI.

```bash
ssh agazhvan2@192.168.2.20
export ROS_DOMAIN_ID=17
source /opt/ros/humble/setup.bash
source ~/lunar_ws/install/setup.bash
```

> `export ROS_DOMAIN_ID=17` **before** launching. A node started without it
> sits on domain 0 and is invisible to the laptop.

### Teleop (always — this is what makes the rover drivable)

```bash
ros2 launch rover_teleop joy_teleop.launch.py
```

### Localization

```bash
ros2 launch rover_localization localization.launch.py
```
Also available from the UI's **LOCALIZATION** button.

### Other stacks

```bash
ros2 launch niti_lidar_bringup lidar.launch.py
ros2 launch niti_lidar_bringup slam.launch.py
ros2 launch niti_lidar_bringup autonomy.launch.py
ros2 launch rover_bringup perception.launch.py
ros2 launch rover_bringup camera.launch.py
ros2 launch ov9281_camera rear_camera.launch.py
```

---

## 2. Laptop — three terminals

Every terminal starts with the same two lines:

```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=17
```

### Terminal 1 — verify the link

```bash
ping -c 3 192.168.2.20
ros2 topic list
```

✅ Rover topics listed → continue.
❌ Only `/parameter_events` and `/rosout` → **stop and fix** (§5). Nothing
downstream works until this passes.

### Terminal 2 — joystick

```bash
ls /dev/input/js*          # joystick present?
ros2 run joy joy_node
```

Verify from Terminal 1 while moving the stick:
```bash
ros2 topic echo /joy
```

### Terminal 3 — Mission Control UI

```bash
cd ~/Agalvaan_UI
PYTHONPATH=src python3 -m mission_control.main
```

Expected header:

| Badge | Expected |
| :--- | :--- |
| `ROVER LINK` | `CONNECTED` green (needs `/rover/heartbeat`) |
| `JETSON` | `WAITING` → `CONNECTED` by itself |

---

## 3. Inside the UI

| Control | Does |
| :--- | :--- |
| **START STREAM** | Starts the in-UI players **and** launches the viewer pipelines from `~/frontcamera.txt` and `~/backcamera.txt` |
| **STOP STREAM** | Stops both, and kills the viewer processes |
| **LOCALIZATION** | Runs the localization launch over SSH, in a new terminal tab if the current shell is busy |
| **EXCAVATE / DUMP / ONE CYCLE AUTO** | **MOCK** — acknowledge only, command nothing. Marked amber. |
| **+ SHELL** | Another SSH shell, so a running launch never blocks you |
| **^C** | Ctrl-C to the active tab only |
| **E-STOP** | Latches; `RELEASE E-STOP` to clear |

### Camera pipeline files

Read fresh on every START STREAM — edit between attempts, no restart needed.

```bash
cat ~/frontcamera.txt    # D456, RTSP H.264
cat ~/backcamera.txt     # back camera, MJPEG over TCP :5000
```

Test a pipeline by hand before blaming the UI:
```bash
bash ~/frontcamera.txt
bash ~/backcamera.txt
```

---

## 4. Checks and diagnostics

```bash
# ROS graph
ros2 topic list -t                      # topics WITH message types
ros2 node list
ros2 topic hz /joy                      # joystick rate (expect ~20-50 Hz)
ros2 topic hz /rover/heartbeat          # link health
ros2 topic echo /imu/data --once
ros2 topic info /joy --verbose          # expect exactly ONE publisher

# Network
ping -c 3 192.168.2.20
ss -tulpn | grep -E "8554|5000"         # RTSP / MJPEG ports
iwconfig 2>/dev/null | grep -i quality  # wifi signal

# Jetson health (over SSH)
jtop
tegrastats
ls -l /dev/ttyUSB*                      # ESP32 present?
```

### Tests

```bash
cd ~/Agalvaan_UI
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 -m pytest -q
```

> The system `pytest` is 6.2.5 and ignores this project's `pythonpath`
> setting, which is why `PYTHONPATH=src` is passed explicitly. `pip install
> -e ".[dev]"` in a venv removes the need.

---

## 5. When something is wrong

### Laptop sees no rover topics

In this order — it is nearly always one of the first two:

```bash
echo $ROS_DOMAIN_ID                     # must be 17 on BOTH machines
ssh agazhvan2@192.168.2.20 'echo $ROS_DOMAIN_ID'
ping -c 3 192.168.2.20
sudo ufw status                         # sudo ufw disable for bench testing
```

Multicast discovery check — laptop first, then the Jetson:
```bash
ros2 multicast receive                  # laptop
ros2 multicast send                     # Jetson
```
If that fails, discovery cannot work. Fix is the unicast peer list (see
`comms_architecture.md` §4.2).

### `ROVER LINK` stays `CONNECTING` but telemetry flows

The badge keys on `/rover/heartbeat`. If your launch files do not publish it
yet, this is expected. Check with `ros2 topic hz /rover/heartbeat`.

### Telemetry cards read `--` / `NO DATA` with a healthy link

A **topic name mismatch**, not a bug. The UI subscribes to the names in
`rover_config.yaml` under `ros_topics:`. FAST_LIO publishes `/Odometry`
(capital O) and the EKF publishes `/odometry/filtered`, neither of which is
`/odom`. Compare and remap:

```bash
ros2 topic list -t
```

### Video: UI panes stay black

```bash
bash ~/frontcamera.txt                  # does the pipeline work standalone?
ss -tulpn | grep 8554                   # is the Jetson serving RTSP?
```
A pipeline in `cameras.*.uri` **must end in `appsink`** — OpenCV cannot read
from `autovideosink` or `fpsdisplaysink`. Those are only for the standalone
viewer files.

### Rover will not move

```bash
ros2 topic hz /joy                      # laptop publishing?
ros2 topic info /joy --verbose          # ONE publisher, and a subscriber on the Jetson?
ls -l /dev/ttyUSB*                      # ESP32 enumerated?
```
Teleop stops by design after ~0.5 s of `/joy` silence — `joy_serial_node`
sends `'S'`, and the ESP32 has its own 500 ms timeout.

---

## 6. Competition day

| | |
| :--- | :--- |
| ☐ | **Laptop Wi-Fi OFF**, wired to the GS305. §7.6.6 — a wireless backchannel is **disqualification** |
| ☐ | Second laptop wired too |
| ☐ | SSID changed to the assigned `Team_##` (§7.1.4) |
| ☐ | 2.4 GHz on **Channel 1**, 20 MHz, no bonding (§7.5.1, §7.1.9) |
| ☐ | `ROS_DOMAIN_ID=17` both machines |
| ☐ | Passwordless SSH verified |
| ☐ | Both camera pipelines tested standalone |
| ☐ | Power logger between battery and kill switch (§4.4.3) |
| ☐ | E-Stop pressed and released — everything returns |

In the RoboPits: **5 GHz only, 2.4 GHz off**. Transmitting on Channel 1 in the
pits is a warning, then **−500 points**, then disqualification (§7.4.5).
