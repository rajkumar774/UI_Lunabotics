# Rover Mission Control — Video Subsystem Architecture & Technical Audit

## 1. Executive Summary & Chosen Architecture

The selected camera streaming architecture is a **Hybrid Dedicated RTSP / H.264 Video Transport with a Decoupled PySide6 Receiver and Stale Frame Dropping**.

```
+-----------------------------------------------------------------------------------+
|               NVIDIA Jetson Orin Nano (Ubuntu 22.04 LTS / ROS 2)                  |
|                                                                                   |
|  [Front Camera (CSI / USB V4L2)]                                                  |
|                 │ (Raw NVMM / YUYV)                                               |
|                 ▼                                                                 |
|  [GStreamer Pipeline: nvv4l2h264enc / x264enc]  <-- Hardware Accelerated H.264   |
|                 │ (H.264 Elementary Stream, Zerolatency, CBR 2.5 Mbps)            |
|                 ▼                                                                 |
|  [RTSP Stream Server (MediaMTX / rtsp-simple-server)]                             |
+─────────────────│─────────────────────────────────────────────────────────────────+
                  │ Dedicated RTSP / UDP Stream (rtsp://<rover_ip>:8554/front_camera)
                  │ Bandwidth: ~2.5 Mbps | Design Target Latency: < 100 ms (Unverified)
+─────────────────│─────────────────────────────────────────────────────────────────+
|                 ▼                                                                 |
|  [Network Ingest & Hardware Decoder (OpenCV / PyAV Ingest Engine)]                |
|                 │ (BGR24 Decoded Frames)                                          |
|                 ▼                                                                 |
|  [BoundedFrameBuffer (Capacity: 2)]  <-- Drops Stale Frames if UI Falls Behind   |
|                 │                                                                 |
|                 ├──────────────────────────────┐                                  |
|                 ▼                              ▼                                  |
|  [StreamStatisticsTracker]           [VideoWorker (QThread)]                      |
|  - Ingestion / Displayed FPS                   │                                  |
|  - Latency (Sim/Unmeasured)                    │ Qt Signals (QueuedConnection)   |
|  - Dropped Frames / Uptime                     ▼                                  |
|                              [LiveVideoPlayerWidget (Qt Main UI Thread)]          |
|                              - 16:9 Aspect Ratio Locked Canvas                    |
|                              - 🔴 LIVE HUD Overlay (YouTube Style)               |
|                              - Fullscreen Mode / Status Overlays                  |
|                                                                                   |
|                      Mission Control (Windows Laptop)                             |
+-----------------------------------------------------------------------------------+
```

---

## 2. Evaluation of Streaming Alternatives

| Architecture | End-to-End Latency Target | Bandwidth (720p@30) | Jetson CPU/GPU Impact | Windows / PySide6 Fit | Stale Frame Dropping | Overall Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1. ROS 2 `Image` (Raw)** | High ($>250\text{ ms}$) | Critical ($>660\text{ Mbps}$) | High CPU (DDS serialization) | Direct | Poor (DDS buffer queues) | **REJECTED** (Saturates WiFi/LAN). |
| **2. ROS 2 `CompressedImage`** | Moderate ($120\text{--}180\text{ ms}$) | Medium ($15\text{--}25\text{ Mbps}$) | Medium (JPEG per frame) | Native | Fair | **FALLBACK ONLY** (Higher bandwidth than H.264). |
| **3. MJPEG over HTTP** | Moderate ($150\text{--}220\text{ ms}$) | High ($20\text{--}35\text{ Mbps}$) | High CPU per frame | Simple | Fair | **REJECTED** (Inefficient compression). |
| **4. Dedicated RTSP + H.264 (Selected)** | **Low ($<100\text{ ms}$ Target)** | **Optimal ($2\text{--}3.5\text{ Mbps}$)** | **Minimal (Jetson NVENC Hardware)** | **Excellent (OpenCV/PyAV/Qt)** | **Guaranteed (Buffer limit = 2)** | **SELECTED (Optimal Balance)**. |
| **5. Raw RTP/UDP H.264** | Very Low ($<80\text{ ms}$ Target) | Optimal ($2\text{--}3.5\text{ Mbps}$)** | Minimal (Jetson NVENC) | Moderate | Manual SDP configuration | **STRONG SECONDARY** (More brittle session management). |
| **6. WebRTC** | Low ($80\text{--}120\text{ ms}$) | Optimal ($1.5\text{--}3\text{ Mbps}$) | Moderate | Complex (Signaling server/C++ libs) | Handled by WebRTC engine | **REJECTED** (Over-engineered for LAN robotics). |
| **7. QtMultimedia (QMediaPlayer)** | Variable ($180\text{--}400\text{ ms}$) | Optimal ($2\text{--}3.5\text{ Mbps}$) | Minimal | Built-in | Hard to drop frames directly | **REJECTED** (Opaque DirectShow pipeline). |

---

## 3. Important Latency Notice & Measurement Methodology

> [!WARNING]
> **Latency Status: UNVERIFIED THEORETICAL DESIGN ESTIMATE**.
> Any previous figures (e.g., $65\text{--}95\text{ ms}$) represent theoretical design targets under optimal network conditions. **No actual end-to-end latency has been measured yet on real Jetson hardware, physical cameras, or arena RF links.**
> In the UI, real camera streams will explicitly display `LATENCY: UNMEASURED` until verified.

### Future End-to-End Latency Measurement Methodology
To establish certified glass-to-glass latency once hardware is integrated:
1. **Optical Visual Millisecond Timer Method**:
   - Run a high-precision millisecond stopwatch on the Mission Control monitor.
   - Point the rover front camera directly at the monitor displaying the stopwatch.
   - Capture a high-speed photo (or video frame) showing both the source stopwatch and the decoded player widget simultaneously.
   - $\text{Glass-to-Glass Latency} = \text{Time}_{\text{Source Stopwatch}} - \text{Time}_{\text{Mission Control Player}}$.
2. **Hardware Synchronized Timestamp Embedding**:
   - Embed hardware sensor capture timestamps into SEI (Supplemental Enhancement Information) metadata or ROS topic headers, comparing against PTP / NTP synchronized system clocks.

---

## 4. Ingestion Engine Audit: OpenCV vs. GStreamer on Windows

### OpenCV `VideoCapture` Considerations
- **Current Prototype Backend**: OpenCV with FFmpeg backend (`cv2.CAP_FFMPEG`).
- **Optimization Applied**: Configured low-latency environment flags:
  ```python
  os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
      "rtsp_transport;udp|fflags;nobuffer|max_delay;0|flags;low_delay"
  )
  ```
- **Known Windows Limitation**: Standard OpenCV builds on Windows may still buffer 1-2 frames internally in the FFmpeg demuxer.
- **Production Recommendation**: If OpenCV exhibits frame buffering jitter during hardware trials, swap the backend in `OpenCVVideoSource` to a direct GStreamer pipeline string (`rtspsrc location=... latency=0 ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! appsink drop=1`) or PyAV packet-level ingestion.

---

## 5. MediaMTX Architecture & Operational Role

```
[GStreamer RTSP Client Sink] ──(Publish: localhost:8554/front_camera)──> [MediaMTX Daemon]
                                                                               │
       ┌───────────────────────────────────────────────────────────────────────┴─────────────────────────────────────────┐
       ▼                                                                                                                 ▼
[Mission Control (UDP/TCP Read)]                                                                 [Secondary Diagnostics / Recorder]
```

### Operational Benefits:
1. **Publisher Decoupling**: If Mission Control disconnects, restarts, or drops packets, the Jetson GStreamer pipeline does NOT crash or terminate. MediaMTX maintains the RTSP mount point continuously.
2. **Multi-Client Fanout**: Allows concurrent viewing on Mission Control and secondary recording/debugging stations without increasing camera encode load on the Jetson.
3. **Transport Flexibility**: Supports RTSP over UDP (minimal latency on clean LAN) with automatic fallback to RTSP over TCP if network firewalls/dropouts require it.
4. **Port Configuration**: Standard RTSP Port `8554`.

---

## 6. Hardware Integration Pre-Flight Checklist

Before executing any commands on the physical NVIDIA Jetson Orin Nano, the following parameters must be verified with the hardware engineering team:

- [ ] **Camera Interface**: USB UVC (V4L2 `/dev/video0`) vs. CSI (MIPI via `nvarguscamerasrc` `/dev/video*`).
- [ ] **Exact Camera Model & Driver**: Sensor resolution capabilities, supported framerates (e.g. 720p@30 vs 1080p@30), pixel format (YUYV vs MJPG vs NV12).
- [ ] **JetPack / L4T Version**: JetPack 5.1.x (L4T 35.x) vs. JetPack 6.x (L4T 36.x) to ensure compatible GStreamer encoder plugin (`nvv4l2h264enc` vs `x264enc`).
- [ ] **Network Configuration**: Fixed static IP of Jetson (`192.168.1.100`), subnet masks, and open firewall port `8554` (UDP/TCP).
- [ ] **Bandwidth Budget**: Dedicated bandwidth allocation for video ($2.5\text{--}4\text{ Mbps}$) within the total local WiFi link.
