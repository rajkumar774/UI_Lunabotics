# Rover ↔ Mission Control Communications Architecture

**Target:** 2026 NASA International Lunabotics Championship, UCF Exolith Lab, 26–29 Oct 2026.
**Scope:** how the Jetson Orin Nano and the MCC laptop talk to each other in every phase of a
run — command execution, teleoperation, and telemetry. The physical network and the rover
itself are already built; this document specifies the **software communication layer** on top
of them.

> **Written 11 Oct 2026 — 15 days to check-in, 17 to Official Run 1.** That deadline is the
> single biggest constraint on everything below. Where an elegant option and a boring option
> both work, this document picks the boring one and says so.

---

## 1. What the rulebook fixes for us

These are not design choices. They are constraints, and several carry disqualification.

| Rule | Consequence for comms |
| :--- | :--- |
| §7.6.6 Network to the robot **only** via the WAP and the MCC cable. "Any backchannel wireless connections via laptop or cellphone … expressly forbidden and will result in disqualification." | **The MCC laptop's Wi-Fi radio must be hard-off.** It is wired to the EXOLITH switch. This is a pre-run checklist item, not a preference. |
| §6.1.2 Setup period is **10 minutes** for laptops, routers, comms and initial checkout | Bring-up must be automatic and observable. There is no time to debug a stack that needs coaxing. |
| §6.1.4.1 Robot has **5 minutes to move** once the timer starts, or the run ends | Teleop must be live within seconds of the timer, independent of anything slow or fragile. |
| §6.1.4.2 **Loss of Comm** is a run-ending anomaly | The link is scored infrastructure. Redundancy on the control path is worth real points. |
| §6.1.3.3 Telerobotic operators may use only "data and video originating from the robot and the competition video monitors" | All situational awareness is our own stream. No external sources. |
| §5.2.4 Arena cameras: **0 used = 120 pts**, 1 = 60, 2 = 0 | Our own video is worth **240 points across two runs**. On the example scorecard (470 total) that is over half a run. Video quality is a scoring system, not a convenience. |
| §5.6 / §6.2 Autonomy is **hands-free**; telemetry viewing is explicitly allowed | Telemetry must keep flowing with zero operator input, and the UI must not be able to influence the rover while in autonomy. |
| §4.3.3 Resetting an E-Stop alone **shall not** resume operation; a second deliberate action is required | The software must mirror the hardware: after an E-Stop clears, the rover comes back **disarmed**. |
| §4.4.3 Power logger sits between battery and kill switch | E-Stop kills the Jetson too (unless separately powered). Plan for a **cold reboot mid-run**, not a graceful one. |
| §7.5.1 2.4 GHz ops on **Channel 1**, monitored; 20 MHz mask, no bonding. 5 GHz allowed but **unmonitored** | 2.4 GHz Ch1 at 20 MHz is a narrow, shared, interference-prone pipe. Budget for it, not for the 5 GHz best case. |
| FAQ: 900 MHz **802.11ah permitted as primary or backup** | A second radio carrying only control traffic is explicitly legal. See §9. |
| §4.2 Onboard comms hardware counts toward the **80 kg** limit; MCC-side equipment does not | Push weight and compute off the rover where the rules allow it. |

---

## 2. Physical topology (as built)

```
         MISSION CONTROL CENTER                    EXOLITH ARENA
  ┌─────────────────────────────────┐     ┌──────────────────────────────┐
  │  MCC Laptop (Ubuntu 22.04)      │     │  Shelf (0–0.5 m above wall)  │
  │  ROS 2 Humble + Agalvaan UI     │     │  ┌────────────────────────┐  │
  │  ⚠ Wi-Fi radio HARD-OFF         │     │  │ Team WAP (Team_##)     │  │
  │            │ eth0 (static)      │     │  │ Ch 1 @ 20 MHz / 5 GHz  │  │
  └────────────┼────────────────────┘     │  └───────────┬────────────┘  │
               │                           │             │ 802.11       │
     ┌─────────┴──────────┐  RJ-45 drop    │             ≈ 3–10 m        │
     │ Netgear GS305      ├────────────────┤             │               │
     │ (EXOLITH-provided) │                │   ┌─────────┴────────────┐  │
     └────────────────────┘                │   │ Jetson Orin Nano     │  │
                                           │   │ ext. antenna (§4.2.4)│  │
         ONE radio hop in the whole path   │   └──────────────────────┘  │
                                           └──────────────────────────────┘
```

**Addressing — static, no DHCP.** DHCP is one more thing that can fail to come up inside a
10-minute window, and a lease renewal mid-run is a gratuitous risk.

| Node | Address | Notes |
| :--- | :--- | :--- |
| WAP | `192.168.50.1` | gateway, DHCP server **disabled** |
| Jetson `wlan0` | `192.168.50.10/24` | static, set in netplan |
| MCC laptop `eth0` | `192.168.50.20/24` | static |
| (reserved) backup radio | `192.168.50.11/24` | see §9 |

Burn these into both machines and into the UI config. Do not rely on mDNS/`.local` — multicast
name resolution over Wi-Fi is exactly as unreliable as DDS multicast (§4.2).

---

## 3. The core decision: four independent planes

**The central principle of this architecture is that losing video must not cost us the rover,
and losing ROS 2 must not cost us teleop.**

Most Lunabotics teams run one transport for everything — ROS 2 topics over Wi-Fi, with teleop
as just another topic. That is simple and it is what UMN does (`joy_node` on the laptop
publishing `/joy` across the link to `rovr_control`). It also means a middleware problem, a
discovery problem, and a video bandwidth problem all present as the same symptom: *the rover
stops responding*, with five minutes on the clock and no way to tell which layer broke.

So: **four planes, chosen so they do not share a failure mode.**

```
   MCC LAPTOP                                              JETSON ORIN NANO
┌───────────────┐                                       ┌───────────────────┐
│ Agalvaan UI   │                                       │                   │
│               │                                       │                   │
│ ① gamepad ────┼──► UDP :5000  20 Hz  ~40 B ──────────►│ safety_node       │
│   ◄───────────┼─── UDP :5001  20 Hz  ~64 B ◄──────────│  (dead-man, arm)  │──► motors
│               │        raw sockets. no ROS. no TCP.   │                   │
│               │                                       │                   │
│ ② telemetry ◄─┼─── ROS 2 Humble, unicast ◄────────────│ rover_bringup     │
│   cards       │       ~40 kbps, best-effort           │  IMU/odom/diag    │
│               │                                       │                   │
│ ③ video ◄─────┼─── RTSP/H.264 CBR ◄───────────────────│ MediaMTX + NVENC  │
│   D456 + PiP  │       2.5 Mbps + 0.8 Mbps capped      │                   │
│               │                                       │                   │
│ ④ terminal ◄──┼──► SSH :22 ◄──────────────────────────│ sshd              │
└───────────────┘                                       └───────────────────┘
```

| Plane | Transport | Rate | If it dies |
| :--- | :--- | :--- | :--- |
| ① **Safety / teleop** | raw UDP, fixed 40-byte frames | 20 Hz | **Rover safe-stops.** Nothing else can cause this. |
| ② **Telemetry / autonomy** | ROS 2 Humble (FastDDS, unicast) | 20 Hz | UI goes stale. Driving unaffected. |
| ③ **Video** | RTSP / H.264, rate-capped | 30 fps | Operator drives on telemetry + arena cams (costs points, not the run). |
| ④ **Shell** | SSH | on demand | Cannot restart nodes remotely. Driving unaffected. |

### 3.1 Why teleop is *not* a ROS 2 topic

This is the one place worth spending the extra day of work.

- **Discovery is a precondition.** A DDS or Zenoh topic only flows after participant discovery
  completes. If the Jetson cold-reboots mid-run (and §4.4.3 means an E-Stop *will* cold-reboot
  it), teleop returns only after the graph re-forms. A UDP socket recovers on the **first
  packet** — no handshake, no state.
- **`rmw_zenoh` is TCP.** Zenoh is the right answer for telemetry over Wi-Fi, but it runs over
  TCP, and TCP head-of-line blocking is actively harmful for teleop: one lost packet stalls the
  stream, then delivers a *burst of stale commands*. The rover lurches. Teleop commands are
  **state, not events** — the newest supersedes all previous ones, and a dropped one should be
  forgotten, not retransmitted.
- **It isolates the blast radius.** A `ros2 launch` that crashes, a node that deadlocks, a
  bad parameter — none of it touches a 200-line safety daemon that does nothing but read a
  socket and write motor setpoints.

The 40-byte UDP frame is small enough to survive a link that cannot carry anything else.

### 3.2 Why keep ROS 2 at all

Because telemetry, perception and autonomy genuinely benefit from it, and we already have it
working end-to-end (see `docs/architecture.md` §3.2). Plane ② is where ROS 2 earns its place.
Just don't put the rover's life on it.

### 3.3 What the four planes actually isolate — and what they do not

The split is worth having, but it is **software-domain isolation only**. Everything below the
software layer is shared by all four planes, and no arrangement of transports changes that.

| Failure | ① teleop | ② telem | ③ video | ④ shell | Isolated? |
| :--- | :--- | :--- | :--- | :--- | :--- |
| ROS node crash / bad launch | **OK** | down | OK | OK | ✅ |
| DDS discovery failure | **OK** | down | OK | OK | ✅ |
| Video encoder crash | **OK** | OK | down | OK | ✅ |
| Video bandwidth burst | **OK** (DSCP EF) | degraded | — | degraded | ✅ |
| SSH session hang | **OK** | OK | OK | down | ✅ |
| Slow ROS startup | **OK** (starts first) | late | late | OK | ✅ |
| **Jetson CPU saturation** | **at risk** | down | down | down | ⚠️ *only with §4.1 scheduling* |
| **RF loss / WAP failure** | down | down | down | down | ❌ |
| **Jetson power loss (E-Stop)** | down | down | down | down | ❌ |
| **wlan0 / IP misconfiguration** | down | down | down | down | ❌ |
| **MCC laptop or UI crash** | down | down | down | down | ❌ |

Six of eleven are genuinely isolated — and they are the *likely* ones, because software
defects outnumber RF and hardware failures by a wide margin over a 30-minute run. But the
headline claim needs correcting:

> **Isolation is the means, not the end. The property that actually holds in all eleven rows
> is _deterministic fail-safe_: whatever breaks, the rover stops safely within a bounded time,
> and recovery is bounded too.**

Read the table that way and it is consistent. In the ✅ rows the rover keeps driving. In the ❌
rows it safe-stops via the onboard dead-man — which needs no link, no laptop and no ROS to
fire — and returns on the first packet once the cause clears. The ❌ rows are **hardware**
problems, and the only real mitigation is redundant hardware: the second radio in §9.

**The ⚠️ row is a genuine hole, and it is ours to close.** An Orin Nano has six A78AE cores
running two NVENC streams, RealSense depth, Nav2 and the ROS 2 graph. Under that load the
safety daemon can be starved by exactly the subsystems it is supposed to be independent of —
so at default priority, plane ① is *not* actually isolated from planes ② and ③. The fix is
cheap and belongs in the systemd unit (§4.1).

---

## 4. Plane specifications

### 4.1 Plane ① — Safety and teleop (the one that must never fail)

**Downlink — MCC → Jetson, UDP `192.168.50.10:5000`, 20 Hz (50 ms):**

| Field | Bytes | Notes |
| :--- | :--- | :--- |
| magic | 4 | `AGLV`, rejects stray traffic |
| version | 1 | layout version; mismatches are dropped, never guessed at |
| session | 2 | randomised per sender process — see note below |
| seq | 4 | monotonic; receiver **discards any packet older than the newest seen** |
| t_sent_ms | 8 | monotonic ms, for one-way latency estimate |
| arm_state | 1 | requested: `SAFE` / `TELEOP` / `AUTONOMY` |
| linear_x | 4 | float, m/s |
| angular_z | 4 | float, rad/s |
| excav_cmd | 1 | drum/actuator enum |
| flags | 2 | software-stop, autonomy-ack, reserved |
| crc16 | 2 | CRC-16/CCITT; IPv4's UDP checksum is optional and weak |
| **total** | **33 B** | ≈15 kbps with UDP/IP/802.11 headers at 20 Hz |

> **The `session` field is not optional, and it was nearly missed.** Without it, a mid-run
> restart of the Mission Control UI resets `seq` to 0 while the rover still holds a
> high-water mark of (say) 12 000 — so **every frame from the restarted laptop is rejected as
> stale, forever.** The rover would ride the dead-man down to `STOPPED` and never come back.
> A changed session id tells the receiver to forget the old high-water mark. Arming is *not*
> inherited across the change: a fresh rising edge is still required.

Sent **unconditionally at 20 Hz**, even when the stick is centred. Silence means "link down",
never "no input". This is what makes the dead-man meaningful.

**Uplink — Jetson → MCC, UDP `192.168.50.20:5001`, 20 Hz:** echoes `seq` and `t_sent_ms` (for
true round-trip latency without clock sync), plus the authoritative `arm_state`, E-Stop line
state, a 1-byte health bitfield, and battery/current. ~64 B.

> **Latency is measured here, not guessed.** Echoing our own timestamp makes RTT exact and
> independent of NTP between the two hosts. The current ROS heartbeat latency (`Header.stamp`
> vs laptop clock) is only meaningful with synchronised clocks — this path has no such
> dependency and should become the number the UI shows.

**Dead-man — on the Jetson, in `safety_node`, never on the laptop:**

| Silence | Action | Rationale |
| :--- | :--- | :--- |
| > **300 ms** (6 missed) | Velocities → 0, stay **armed** | Rides out normal Wi-Fi jitter on Ch 1 without a jerky stop. Panther's 20 ms heartbeat would false-trip constantly on this link. |
| > **2 s** | **Disarm.** Re-arm needed | Beyond a transient. Operator must deliberately resume. |
| > **10 s** | Latch `STOPPED`, log, hold | Matches §6.1.4 Loss of Comm. Rover is parked and safe for judges. |

The ladder **continues past `SAFE` into `STOPPED`**: a ten-second outage is a reportable
mission anomaly (§6.1.4.2 — the team should tell the MCJ), which the operator needs
annunciated differently from an ordinary disarm. It latches exactly once.

Three stages, not one, because a single hard threshold forces a choice between nuisance stops
and unsafe coasting. Autonomy relaxes the 300 ms rule (the rover is driving itself) but
**keeps the 10 s rule** — an uncontrolled autonomous rover with a dead link is the one outcome
nobody wants in the arena.

**Priority on the air.** Mark plane ① `DSCP EF (46)` and video `AF21`. Consumer APs honour
WMM, which maps EF to the Voice access category — teleop then wins contention against a video
burst. One `iptables`/`tc` line each; one of the cheapest reliability wins available.

**Priority on the CPU — this is what closes the ⚠️ row in §3.3.** Air priority is pointless if
the daemon never gets scheduled. The safety daemon must outrank the encoder and the ROS graph
on the Jetson itself:

```ini
# /etc/systemd/system/rover-safety.service
[Service]
ExecStart=/usr/bin/python3 /opt/rover/rover_safety_daemon.py
CPUSchedulingPolicy=fifo      # real-time; preempts ROS/NVENC
CPUSchedulingPriority=80      # below kernel threads, above everything of ours
CPUAffinity=5                 # a core the ROS graph is kept off
IOSchedulingClass=realtime
LimitMEMLOCK=infinity         # daemon mlockall()s; never swapped out
OOMScoreAdjust=-900           # last process the OOM killer touches
Restart=always
RestartSec=1
```

Pair it with `CPUAffinity=0-4` on `rover-ros.service` and `rover-video.service` so core 5 stays
reserved. Without this, a `nice`-level daemon competing with two NVENC sessions can miss its
20 Hz deadline under exactly the load that matters — and the dead-man then trips on a link
that was perfectly healthy.

### 4.2 Plane ② — Telemetry over ROS 2

ROS 2 Humble, `ROS_DOMAIN_ID=17`, already implemented (`ROS2CommsClient` + `ROS2WorkerThread`).
Two hardening changes, both config-only:

**(a) Kill multicast discovery.** DDS's Simple Participant Discovery floods multicast, and
802.11 sends multicast at the lowest basic rate with no acknowledgement — it is the least
reliable traffic class on the link. With exactly two hosts there is nothing to discover.
Pin a unicast peer list in a FastDDS profile XML:

```xml
<participant profile_name="arena" is_default_profile="true">
  <rtps><builtin>
    <metatrafficUnicastLocatorList>
      <locator><udpv4><address>192.168.50.10</address></udpv4></locator>
    </metatrafficUnicastLocatorList>
    <initialPeersList>
      <locator><udpv4><address>192.168.50.10</address></udpv4></locator>
      <locator><udpv4><address>192.168.50.20</address></udpv4></locator>
    </initialPeersList>
    <discovery_config><ignoreParticipantFlags>FILTER_SAME_PROCESS</ignoreParticipantFlags></discovery_config>
  </builtin></rtps>
</participant>
```
`export FDS_DEFAULT_PROFILES_FILE=/opt/rover/arena_dds.xml` on **both** hosts.

**(b) Everything stays BEST_EFFORT.** Already done — see `worker.py::_setup_interfaces`.
Reliable QoS over a lossy radio produces retransmit storms exactly when the link is already
struggling, which is the worst possible time.

> **Why not `rmw_zenoh`?** It is genuinely the better middleware for Wi-Fi — a router instead
> of multicast flooding, and measured discovery-overhead reductions of 97–99.9%. Binaries exist
> for Humble (`ros-humble-rmw-zenoh-cpp`). **But it is an RMW swap 15 days before a
> competition**, and there is a known footgun: Humble and Jazzy nodes discover each other and
> then silently fail to exchange data (message type hash mismatch from Iron onward). Do it in
> the off-season, with time to test. The unicast-peer change above captures most of the benefit
> for an afternoon of work and zero new dependencies.

**Never send pointclouds over the link.** The D456's depth stream is for onboard consumption —
Nav2, obstacle detection, berm alignment. Across the air send only derived products: an
obstacle list, a small occupancy delta, a berm-alignment error. Tens of bytes instead of tens
of megabits. This is the single biggest bandwidth trap in a RealSense-based design.

### 4.3 Plane ③ — Video

Worth **240 points across two runs** (§5.2.4), so it is treated as a scored subsystem.

| Stream | Source | Encode | Resolution | Bitrate |
| :--- | :--- | :--- | :--- | :--- |
| Main | D456 colour | NVENC H.264, zerolatency, **CBR** | 1280×720 @ 30 | **2.5 Mbps** |
| PiP | Front cam | NVENC H.264, CBR | 640×360 @ 15 | **0.8 Mbps** |

**CBR, not VBR.** A variable-bitrate encoder bursts on scene change — and in this arena every
scene change is a dust plume during excavation, i.e. the encoder spikes at the exact moment
the operator most needs a responsive control link. Fixed ceiling, always.

Served by MediaMTX on the Jetson (`rtsp://192.168.50.10:8554/{d456,front}`), already supported
by `OpenCVVideoSource` with low-latency FFmpeg flags.

**A `LOW BW` mode is a required feature, not a nicety.** One operator action drops both streams
to 480p/10 fps/600 kbps total. The decision tree during a degraded run must be *"shed video
quality"*, never *"fall back to the arena cameras"* — the latter costs 60–120 points.

UMN uses NVENC **AV1** on an AGX Orin, which is ~30–40% more efficient than H.264. Worth having
on the roadmap; not worth re-encoding the pipeline this month, and Orin **Nano** encoder support
differs from AGX.

### 4.4 Plane ④ — Shell

SSH, already implemented with multi-shell tabs and auto-connect (`comms/jetson_ssh.py`). It
carries operator commands only — starting nodes, `ros2 topic hz`, `jtop`, reboots. **Nothing
read from this plane is ever written into `StateManager`**, so a shell problem can never
corrupt rover state.

---

## 5. Rover arm-state machine (authoritative, on the Jetson)

The laptop **requests** states. The Jetson **owns** them. The UI renders what the Jetson
reports — never what the operator clicked.

```
            power-on / E-Stop reset / watchdog expiry
                              │
                              ▼
                       ┌─────────────┐
            ┌─────────►│    SAFE     │  motors inhibited, listening
            │          └──────┬──────┘
            │        operator presses ARM  (deliberate, second action — §4.3.3)
            │                 ▼
            │          ┌─────────────┐   300 ms silence → zero velocity, stay here
   disarm   ├──────────│ TELEOP_ARMED│   2 s silence → SAFE
   / 2 s    │          └──────┬──────┘
            │       announce to MCJ, press AUTONOMY
            │                 ▼
            │          ┌─────────────┐   teleop dead-man relaxed
            ├──────────│  AUTONOMY   │   10 s link loss → STOPPED
            │          └──────┬──────┘   any teleop input → immediate TELEOP_ARMED
            │                 │
            │     software stop / 10 s link loss / fault
            │                 ▼
            │          ┌─────────────┐
            └──────────│   STOPPED   │  latched. only a deliberate re-arm leaves.
                       └─────────────┘
```

Two points that come straight from the rules:

- **`SAFE` after an E-Stop reset is mandatory.** §4.3.3: restoring power must not restore
  motion. Our current UI latches E-Stop and offers `RELEASE E-STOP` — that returns to `SAFE`,
  and a **separate ARM press** is then required. *(Gap: the ARM step does not exist yet — §8.)*
- **Any teleop input during `AUTONOMY` instantly reverts to `TELEOP_ARMED`** and is logged with
  a timestamp. Under §6.2 that aborts the autonomy attempt, and the team must announce the
  failure before resuming manual control. The log is our own evidence of exactly when hands
  went back on.

---

## 6. Scenario walkthroughs

### Scenario 0 — Pits (practice, days before)

The RoboPits are a **hostile DDS environment**: many teams, 5 GHz, same subnet possible. A
neighbouring team on the default `ROS_DOMAIN_ID=0` can silently join our graph. Mitigations:
our non-default domain (`17`) plus the unicast peer list in §4.2 — which also makes pit testing
representative of arena behaviour.

Per §7.4, pit routers run **5 GHz with 2.4 GHz off**; Channel 1 transmission in the pits earns
a warning, then **−500 points**, then disqualification.

### Scenario 1 — The 10-minute setup window ⭐

This is the scenario that decides whether the other nine ever happen. Target: **rover online and
teleop-verified in under 3 minutes**, leaving 7 minutes of margin for the unexpected.

```
T+0:00   Arena crew places rover, powers on. MCC crew already seated (§3.2.13).
T+0:05   MCC: confirm laptop Wi-Fi is OFF (DQ check), launch UI.
T+0:40   Jetson boots. systemd brings up, in dependency order:
            1. rover-net      wlan0 assoc → static IP → ping WAP
            2. rover-safety   UDP :5000 listener. Rover is SAFE but LISTENING.   ◄── first
            3. rover-ros      ros2 launch rover_bringup (IMU, odom, diag, heartbeat)
            4. rover-video    GStreamer → MediaMTX
         Every unit: Restart=always, RestartSec=2.
T+1:10   UI: TELEOP badge → LINKED (plane ① echo received)
T+1:20   UI: ROVER LINK → CONNECTED (plane ② heartbeat)
T+1:30   UI: JETSON → CONNECTED (plane ④ auto-connect)
T+1:40   UI: video LIVE, both streams
T+2:00   ARM. Verify each drive direction and the excavation actuator.
T+2:30   DISARM. Report "ready" to the MCJ (§6.1.2.2).
T+2:30 → T+10:00   margin.
```

**`rover-safety` starting before `rover-ros` is deliberate.** If the ROS stack is slow or
broken, we still have a drivable rover and a scoreable run. The reverse ordering gives us a
rover that can talk but not move.

**The UI needs a readiness checklist** for this window — four plane indicators plus
*armed / E-Stop clear / both video streams / battery*, in one GO/NO-GO panel. Under time
pressure an operator should read one panel, not hunt across the window. *(Gap — §8.)*

Note §6.1.2.5: arena situational-awareness cameras may be used **during setup without
penalty**. Use them to confirm rover placement and orientation, then stop — the 120 points are
for usage during the *run*.

### Scenario 2 — Nominal teleop

All four planes up. Gamepad → plane ① at 20 Hz → `safety_node` → motor controllers.
Telemetry at 20 Hz on plane ②, UI repaints capped at 30 FPS. RTT shown from the plane ① echo.
Aggregate ~3.5 Mbps.

### Scenario 3 — Autonomy engaged

Operator announces to the MCJ, presses `AUTONOMY`, **hands off everything** (§6.2).

- Plane ① **keeps running at 20 Hz** with zeroed velocities and `arm_state = AUTONOMY`. It is
  now a pure liveness channel. This is not operator input and does not violate hands-free.
- Plane ② telemetry continues — explicitly permitted by §5.6.1 and §5.6.5.
- Teleop dead-man relaxed; the 10 s link-loss rule still applies.
- Any gamepad deflection → instant `TELEOP_ARMED`, logged. Autonomy attempt is over; announce
  the failure before resuming (§6.2 general rules).

### Scenario 4 — Degraded link (packet loss, dust, interference)

Symptom order as a link degrades: **video tears first, telemetry goes stale second, teleop
last** — by design, and that ordering is itself the diagnostic.

| Observation | Response |
| :--- | :--- |
| Plane ① RTT > 150 ms or loss > 5% | Operator drives slower; watch for the 300 ms stop |
| Video stuttering | `LOW BW` → 600 kbps total |
| Telemetry stale, teleop fine | Keep driving. Plane ② is not load-bearing. |
| Still degraded | Park in the excavation zone, let the link recover with the radio idle |

### Scenario 5 — Link loss

300 ms → zero velocity. 2 s → disarm. 10 s → `STOPPED` latched.
UI shows all four planes dropping in their own time, which tells us *where* the break is:
all four at once = radio/WAP. Plane ② alone = ROS stack. Plane ③ alone = video pipeline.
If unrecoverable, §6.1.4.2 says the responsible call is to tell the MCJ and end the run.

### Scenario 6 — Jetson cold reboot mid-run

The realistic case after a physical E-Stop (§4.4.3 puts the logger before the kill switch, so
the Jetson loses power with it).

```
E-Stop pressed → battery isolated → Jetson dies → all four planes drop
Operator twists to release → power restored → ~40 s boot
rover-safety up          → plane ① LINKED, state = SAFE
rover-ros / rover-video  → planes ②③ return
Operator presses ARM     → TELEOP_ARMED        ◄── the required second action
```

`Restart=always` on every unit means no SSH intervention is needed. Plane ① returns **first and
without a handshake** — which is the whole argument for §3.1 in one scenario.

### Scenario 7 — Mission end and egress

At `00:00` the UI stops the clock. Per §6.1.5 the rover may finish an in-progress dump, and may
need to move again to unload before removal — so **do not tear anything down.** §6.1.5.2
forbids disconnecting until the MCJ directs it. The UI's E-Stop and `DISCONNECT` should both be
behind a confirmation during this window.

---

## 7. Bandwidth budget

2.4 GHz Ch 1 at 20 MHz, single spatial stream, in a dusty metal-walled arena. Nominal PHY rates
are irrelevant; plan against a **conservative ~10 Mbps of usable goodput** and leave headroom.

| Plane | Steady | Peak | Priority |
| :--- | ---: | ---: | :--- |
| ① Teleop (both directions, 20 Hz) | 20 kbps | 25 kbps | **DSCP EF** |
| ② Telemetry (20 Hz) | 40 kbps | 80 kbps | AF31 |
| ③ Video main (CBR) | 2 500 kbps | 2 500 kbps | AF21 |
| ③ Video PiP (CBR) | 800 kbps | 800 kbps | AF21 |
| ④ SSH | ~5 kbps | 200 kbps | BE |
| **Total** | **≈ 3.4 Mbps** | **≈ 3.6 Mbps** | |

≈3.4 Mbps against ~10 Mbps usable is roughly **3× headroom**, and lines up with the ≤4 Mbps
efficiency target NASA used in earlier Robotic Mining Competition guidance. `LOW BW` mode takes
the total to ~1.3 Mbps for a badly degraded link.

The control plane is **0.6%** of the budget. That is the point: it can get through almost
anything, and nothing we do to video can starve it.

---

## 8. What this means for the existing Agalvaan UI

Already built and tested against a live ROS 2 graph:

- ✅ Plane ② — `ROS2CommsClient` / `ROS2WorkerThread`, unicast-safe QoS, heartbeat watchdog
- ✅ Plane ③ — dual camera pipeline, independent workers, PiP
- ✅ Plane ④ — SSH with multi-shell tabs, auto-connect, busy detection
- ✅ `ROVER LINK` and `JETSON` status, latched E-Stop, `StateManager` as single source of truth

Gaps this architecture opens, in priority order:

| # | Gap | Why it matters | Size |
| :-- | :--- | :--- | :--- |
| 1a | ~~Plane ① transport + state machine~~ | **Done** — `comms/teleop/{protocol,safety,link}.py`, `tools/rover_safety_daemon.py`, 47 tests incl. real-socket end-to-end | ✅ |
| 1b | **Gamepad input source** — nothing yet drives `TeleopLink.set_velocity()` | No physical control surface. `evdev` not installed; no pad attached to this laptop. | ~half a day |
| 1c | **Wire plane ① into the UI** — ARM button, TELEOP badge, RTT/loss display | The link exists but the operator cannot see or use it | ~half a day |
| 2 | **No ARM step** — E-Stop release returns straight to operational | §4.3.3 compliance | ~2 h |
| 3 | **No readiness checklist panel** | The 10-minute window | ~3 h |
| 4 | **No `TELEOP` badge** (we show ROVER LINK + JETSON only) | Operator cannot see the plane that matters most | ~1 h |
| 5 | **No `LOW BW` control** | Directly defends 120 pts/run | ~2 h + Jetson side |
| 6 | DDS unicast profile not deployed | Removes the worst Wi-Fi failure mode | ~2 h, config only |
| 7 | Latency shown comes from `Header.stamp` (needs NTP) | Plane ① echo gives true RTT with no clock sync | ~1 h once ① exists |

**Suggested order for the remaining 14 days:** 1b → 1c → 2 → 4 → 6 → 3 → 5 → 7, with a **hard code freeze
five days out (21 Oct)** and the remaining time on full-dress rehearsals: cold-boot to driving,
on the real radio, with someone pulling the Jetson's power without warning.

---

## 9. Optional: the backup control radio

The FAQ explicitly permits **900 MHz 802.11ah as a primary or backup system**, and UWB at
6.5/8 GHz. Because plane ① is only ~20 kbps and completely self-contained, it can run over a
second radio with no changes to its design — the Jetson simply binds the listener to both
interfaces and accepts whichever packets arrive, deduplicating on `seq`.

That turns "Loss of Comm" — a run-ending anomaly under §6.1.4.2 — into a degraded mode where
video and telemetry are gone but the rover is still drivable and still scoring berm volume.

**Not for this year.** New RF hardware 15 days out needs a comm-check re-pass and risks
unknown interactions. It is the strongest single reliability upgrade for the next cycle, and
plane ① should be designed now so it is a drop-in later.

---

## 10. How this compares to other Lunabotics teams

| Team / project | Their approach | Assessment |
| :--- | :--- | :--- |
| **UMN GOFIRST** (AGX Orin) | ROS 2 across the link; `joy_node` on the laptop → `/joy` → `rovr_control`. GStreamer **NVENC AV1**, Nav2, nvblox, CAN/VESC | Strongest perception stack of the public repos, and AV1 is the right call on bandwidth. But teleop as a DDS topic means middleware failure = loss of vehicle control. |
| **Panther Robotics** (Orin Nano) | Custom **TCP/IP** protocol, ACKs, **20 ms** safety heartbeat | Correct instinct — a dedicated control plane separate from ROS. Two quibbles: TCP head-of-line blocking delivers stale commands in a burst after loss, and a 20 ms heartbeat will false-trip on Ch 1 Wi-Fi jitter. We take the idea and use UDP with a 300 ms first stage. |
| **College of DuPage** | ROS 2 + **PyQt5** GUI, `lunabot_bringup` launch structure | Same stack as ours. Confirms PyQt is a reasonable MCC choice. Safety/deadman design not public. |
| **UHCL Lunar Hawks** | ROS 2 + micro-ROS, autonomous state machine, teleop for recovery | micro-ROS to MCUs is a clean onboard pattern; teleop-as-recovery matches our "teleop in all scenarios" requirement. |
| **CPRT / Robot Web Tools** | `rosbridge` + browser dashboard | Fast to build; adds a JSON/WebSocket hop and a browser to the critical path. Native Qt avoids both. |
| **jsmith-exe/lunabotics** | Plain HTTP + SSE telemetry + MJPEG, deliberately **no** rosbridge | Same anti-dependency instinct as plane ①. MJPEG costs far more bandwidth than H.264. |

**Where this architecture differs:** nobody public separates *all four* concerns by failure
mode. The recurring pattern is one transport for everything, with teleop riding the same
middleware as telemetry. Given that Loss of Comm is explicitly a run-ending anomaly and we get
exactly two 30-minute attempts, isolating the control path is worth more than any feature we
could add with the same effort.

The second difference is treating **video as a scored subsystem** rather than a convenience.
At 120 points per run for using zero arena cameras, a CBR cap and a `LOW BW` fallback are worth
more than a sharper picture that occasionally collapses.

---

## Sources

- 2026 NASA International Lunabotics Championship Guidebook v2 (3 Sep 2026) — §§1.3, 2–8
- [Lunabotics FAQ — Florida Space Institute](https://fsi.ucf.edu/lunabotics/faq/)
- [UMN GOFIRST Robotics — Lunabotics](https://github.com/GOFIRST-Robotics/Lunabotics)
- [Panther Robotics 2025-2026](https://github.com/Asher-Plihal/PantherLunabotics2025-2026)
- [College of DuPage — lunabot_ros](https://github.com/College-of-DuPage-Lunabotics/lunabot_ros)
- [UHCL Lunar Hawks](https://github.com/Matthew-Garcia/NASA-Lunabotics-UHCL-Lunar-Hawks)
- [Minimizing Discovery Overhead in ROS 2 — Zenoh](https://zenoh.io/blog/2021-03-23-discovery/)
- [rmw_zenoh](https://index.ros.org/r/rmw_zenoh) · [zenoh-plugin-ros2dds](https://github.com/eclipse-zenoh/zenoh-plugin-ros2dds)
- [How to solve wireless network issues in ROS 2 — Vulcanexus](https://docs.vulcanexus.org/en/jazzy/rst/tutorials/core/wifi/wifi_issues_tutorial/wifi_issues_tutorial.html)
- [Comparison of Middlewares in Edge-to-Edge and Edge-to-Cloud Communication](https://arxiv.org/pdf/2309.07496)
- [NASA Lunabotics Robotic Mining Competition retrospective](https://ntrs.nasa.gov/api/citations/20200003009/downloads/20200003009.pdf)
