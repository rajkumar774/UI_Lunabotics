"""Live Video Player UI Widget with 16:9 aspect ratio locking and YouTube-style HUD."""

from typing import Optional
from PySide6.QtCore import Qt, QRect, QSize, Signal
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QBrush, QImage, QKeyEvent
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
    QSizePolicy,
    QSplitter,
)

from mission_control.video.frame_buffer import FrameMetadata
from mission_control.video.stats import StreamState, StreamStats


#: Valid picture-in-picture corners.
PIP_CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")

#: Inset margin from the viewport edge, in pixels.
PIP_MARGIN = 14


class VideoCanvas(QWidget):
    """Canvas widget that paints video frames with strict 16:9 aspect ratio and letterboxing.

    An optional second stream is drawn as a picture-in-picture inset inside the
    same 16:9 viewport, so the inset tracks the letterboxed video rather than
    the widget rectangle and never floats out over a black bar.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._current_frame: Optional[QImage] = None
        self._current_state: StreamState = StreamState.DISCONNECTED
        self._error_message: str = ""
        self._reconnect_count: int = 0

        self._pip_frame: Optional[QImage] = None
        self._pip_label: str = ""
        self._pip_corner: str = "top_right"
        self._pip_width_fraction: float = 0.26
        self._pip_enabled: bool = False
        self._pip_live: bool = False
        self._title: str = "ROVER FRONT CAMERA"

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

    def set_frame(self, frame: QImage) -> None:
        self._current_frame = frame
        self.update()

    def set_state(self, state: StreamState, error_msg: str = "", reconnect_count: int = 0) -> None:
        self._current_state = state
        self._error_message = error_msg
        self._reconnect_count = reconnect_count
        self.update()

    def clear_frame(self) -> None:
        self._current_frame = None
        self.update()

    def set_title(self, title: str) -> None:
        """Name shown on the offline placeholder."""
        self._title = title
        self.update()

    # -- Picture-in-picture --------------------------------------------------

    def configure_pip(
        self,
        label: str,
        corner: str = "top_right",
        width_fraction: float = 0.26,
        enabled: bool = True,
    ) -> None:
        """Enables the inset and sets its caption, corner and relative size."""
        self._pip_label = label
        self._pip_corner = corner if corner in PIP_CORNERS else "top_right"
        # Below ~12% the inset is too small to read; above 50% it stops being an inset.
        self._pip_width_fraction = max(0.12, min(0.5, width_fraction))
        self._pip_enabled = enabled
        self.update()

    def set_pip_frame(self, frame: QImage) -> None:
        self._pip_frame = frame
        self._pip_live = True
        self.update()

    def set_pip_state(self, state: StreamState) -> None:
        """Tracks the inset stream so it can show OFFLINE instead of a frozen frame."""
        self._pip_live = state == StreamState.LIVE
        if not self._pip_live:
            self._pip_frame = None
        self.update()

    @property
    def pip_enabled(self) -> bool:
        return self._pip_enabled

    @property
    def pip_is_live(self) -> bool:
        return self._pip_live and self._pip_frame is not None

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        width = self.width()
        height = self.height()

        # Fill background with pure black for letterboxing
        painter.fillRect(0, 0, width, height, QColor(13, 7, 9))

        # Target 16:9 dimensions
        aspect_ratio = 16.0 / 9.0
        if width / max(1, height) > aspect_ratio:
            # Pillarbox (black bars left and right)
            target_h = height
            target_w = int(height * aspect_ratio)
            offset_x = (width - target_w) // 2
            offset_y = 0
        else:
            # Letterbox (black bars top and bottom)
            target_w = width
            target_h = int(width / aspect_ratio)
            offset_x = 0
            offset_y = (height - target_h) // 2

        render_rect = QRect(offset_x, offset_y, target_w, target_h)

        if self._current_state == StreamState.LIVE and self._current_frame is not None:
            # Draw live video frame scaled to 16:9 container
            painter.drawImage(render_rect, self._current_frame)
        else:
            # Draw offline / connecting placeholder state
            self._draw_state_placeholder(painter, render_rect)

        if self._pip_enabled:
            self._draw_pip(painter, render_rect)

        painter.end()

    def pip_rect(self, viewport: QRect) -> QRect:
        """Geometry of the inset within a 16:9 viewport, in widget coordinates."""
        pip_w = max(80, int(viewport.width() * self._pip_width_fraction))
        pip_h = int(pip_w * 9 / 16)

        if "right" in self._pip_corner:
            x = viewport.right() - pip_w - PIP_MARGIN
        else:
            x = viewport.left() + PIP_MARGIN
        if "bottom" in self._pip_corner:
            y = viewport.bottom() - pip_h - PIP_MARGIN
        else:
            y = viewport.top() + PIP_MARGIN

        return QRect(x, y, pip_w, pip_h)

    def _draw_pip(self, painter: QPainter, viewport: QRect) -> None:
        """Paints the inset stream, its border and its caption."""
        rect = self.pip_rect(viewport)
        if rect.width() < 80 or rect.height() < 45:
            return

        painter.fillRect(rect, QColor(10, 5, 7))
        if self.pip_is_live:
            painter.drawImage(rect, self._pip_frame)
        else:
            painter.setPen(QColor(138, 112, 121))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(rect, Qt.AlignCenter, "NO SIGNAL")

        painter.setPen(QPen(QColor(224, 101, 127, 190), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect)

        if not self._pip_label:
            return

        caption_h = 15
        caption = QRect(rect.left(), rect.bottom() - caption_h, rect.width(), caption_h)
        painter.fillRect(caption, QColor(13, 7, 9, 215))
        painter.setPen(QColor(221, 204, 210))
        painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
        painter.drawText(caption.adjusted(5, 0, -5, 0), Qt.AlignVCenter | Qt.AlignLeft, self._pip_label)

        if self.pip_is_live:
            painter.setPen(QColor(255, 95, 95))
            painter.drawText(caption.adjusted(0, 0, -5, 0), Qt.AlignVCenter | Qt.AlignRight, "● LIVE")

    def _draw_state_placeholder(self, painter: QPainter, rect: QRect) -> None:
        """Draws structured status overlay cards when stream is offline/connecting/error."""
        # Subtle viewport background
        painter.fillRect(rect, QColor(23, 15, 19))
        painter.setPen(QPen(QColor(53, 34, 42), 1, Qt.DashLine))
        painter.drawRect(rect.adjusted(1, 1, -1, -1))

        cx = rect.center().x()
        cy = rect.center().y()

        painter.setPen(QColor(176, 154, 161))
        font_title = QFont("Segoe UI", 14, QFont.Bold)
        font_sub = QFont("Segoe UI", 11, QFont.Normal)

        if self._current_state == StreamState.CONNECTING:
            painter.setFont(font_title)
            painter.setPen(QColor(224, 101, 127))  # Rose accent
            msg = "CONNECTING TO ROVER CAMERA..."
            painter.drawText(QRect(cx - 250, cy - 40, 500, 30), Qt.AlignCenter, msg)

            painter.setFont(font_sub)
            painter.setPen(QColor(176, 154, 161))
            sub = f"Negotiating stream link • Attempt #{self._reconnect_count + 1}"
            painter.drawText(QRect(cx - 250, cy, 500, 30), Qt.AlignCenter, sub)

        elif self._current_state == StreamState.ERROR:
            painter.setFont(font_title)
            painter.setPen(QColor(239, 68, 68))  # Red
            painter.drawText(QRect(cx - 250, cy - 40, 500, 30), Qt.AlignCenter, "STREAM ERROR")

            painter.setFont(font_sub)
            painter.setPen(QColor(221, 204, 210))
            err_text = self._error_message or "Connection to camera failed. Auto-retrying..."
            painter.drawText(QRect(cx - 300, cy, 600, 30), Qt.AlignCenter, err_text)

        elif self._current_state == StreamState.NO_SIGNAL:
            painter.setFont(font_title)
            painter.setPen(QColor(245, 158, 11))  # Amber
            painter.drawText(QRect(cx - 250, cy - 40, 500, 30), Qt.AlignCenter, "NO SIGNAL DETECTED")

            painter.setFont(font_sub)
            painter.setPen(QColor(176, 154, 161))
            painter.drawText(QRect(cx - 250, cy, 500, 30), Qt.AlignCenter, "Stream timed out • Retrying stream ingestion...")

        else:  # DISCONNECTED
            painter.setFont(font_title)
            painter.setPen(QColor(138, 112, 121))  # Muted mauve
            painter.drawText(QRect(cx - 250, cy - 40, 500, 30), Qt.AlignCenter, self._title)

            painter.setFont(font_sub)
            painter.setPen(QColor(107, 82, 90))
            painter.drawText(
                QRect(cx - 300, cy, 600, 30),
                Qt.AlignCenter,
                "STREAM OFFLINE • Click 'START STREAM' or connect rover to initialize player",
            )


class LiveVideoPlayerWidget(QFrame):
    """Full-featured YouTube-Live style video player for rover operations."""

    #: Legacy no-argument signals: "start/stop everything". Kept so existing
    #: callers keep working; the per-stream signals below are the new path.
    stream_start_requested = Signal()
    stream_stop_requested = Signal()

    #: Per-stream control. Argument is the stream id: "main" or "pip".
    #: Each camera is started and stopped independently, because a dead back
    #: camera should never cost you the main view.
    stream_start_one = Signal(str)
    stream_stop_one = Signal(str)
    fullscreen_toggled = Signal(bool)

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        camera_title: str = "ROVER FRONT CAMERA",
    ):
        super().__init__(parent)
        self.setObjectName("LiveVideoPlayer")
        self.setFrameShape(QFrame.StyledPanel)

        self._is_fullscreen = False
        #: Authoritative main-stream state, written by handle_state_changed().
        #: The toggle button used to read self._stats.state instead, but stats
        #: arrive on a *different* signal than state transitions -- so after a
        #: stream went LIVE without a stats update, the button read STOP STREAM
        #: while the toggle still believed it was stopped, and pressing STOP
        #: silently restarted the stream instead of stopping it.
        self._main_state = StreamState.DISCONNECTED
        #: None until telemetry says otherwise -- unknown, not 'not localized'.
        self._localized: Optional[bool] = None
        #: Where to put this widget back when leaving fullscreen.
        self._restore_parent = None
        self._restore_splitter = None
        self._restore_index = -1
        self._restore_sizes = None
        self._stats = StreamStats()
        self._camera_title = camera_title
        self._pip_title = "PIP CAM"
        self._main_running = False
        self._pip_running = False

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 1. Top HUD Overlay Bar
        self.top_bar = self._create_top_hud()
        layout.addWidget(self.top_bar)

        # 2. Main 16:9 Canvas
        self.canvas = VideoCanvas(self)
        self.canvas.set_title(self._camera_title)
        layout.addWidget(self.canvas, stretch=1)

        # 3. Bottom HUD Overlay Bar
        self.bottom_bar = self._create_bottom_hud()
        layout.addWidget(self.bottom_bar)

    def _create_top_hud(self) -> QWidget:
        top_widget = QWidget(self)
        top_widget.setObjectName("VideoTopHUD")
        top_widget.setStyleSheet(
            "QWidget#VideoTopHUD { background-color: rgba(14, 18, 26, 0.92); border-bottom: 1px solid #35222a; padding: 4px 12px; }"
        )
        layout = QHBoxLayout(top_widget)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(12)

        # 🔴 LIVE Badge
        self.live_badge = QLabel("● LIVE")
        self.live_badge.setStyleSheet(
            "background-color: #2b171a; color: #ff5f5f; font-weight: 800; font-size: 11px; "
            "padding: 3px 8px; border-radius: 3px; border: 1px solid #7f1d1d;"
        )
        layout.addWidget(self.live_badge)

        # Stream State Text
        self.state_label = QLabel("DISCONNECTED")
        self.state_label.setStyleSheet("color: #b09aa1; font-size: 11px; font-weight: 600;")
        layout.addWidget(self.state_label)

        layout.addStretch()

        # Stream Resolution
        self.resolution_label = QLabel("1280×720 HD")
        self.resolution_label.setStyleSheet(
            "background-color: #2b171e; color: #e0657f; font-family: monospace; font-size: 11px; "
            "font-weight: 700; padding: 2px 6px; border-radius: 3px; border: 1px solid #6d1628;"
        )
        layout.addWidget(self.resolution_label)

        return top_widget

    def _create_bottom_hud(self) -> QWidget:
        bottom_widget = QWidget(self)
        bottom_widget.setObjectName("VideoBottomHUD")
        bottom_widget.setStyleSheet(
            "QWidget#VideoBottomHUD { background-color: rgba(14, 18, 26, 0.92); border-top: 1px solid #35222a; padding: 4px 12px; }"
        )
        layout = QHBoxLayout(bottom_widget)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(16)

        # Stream Title / Camera ID
        self.camera_title = QLabel(self._camera_title)
        self.camera_title.setStyleSheet("color: #ebdde1; font-weight: 700; font-size: 12px; letter-spacing: 0.5px;")
        layout.addWidget(self.camera_title)

        layout.addStretch()

        # Localization indicator, sat next to the stream stats because that is
        # where the operator's eyes already are while driving. Green only while
        # AprilTag fixes are still arriving; it is NOT a link indicator.
        self.localization_led = QLabel("● LOC")
        self.localization_led.setObjectName("LocalizationLed")
        self.localization_led.setToolTip(
            "AprilTag localization\n"
            "green: fixes arriving  ·  red: no fix  ·  grey: unknown")
        self._render_localization_led()
        layout.addWidget(self.localization_led)

        # FPS Display
        self.fps_label = QLabel("-- FPS")
        self.fps_label.setStyleSheet("color: #e0657f; font-family: monospace; font-weight: 700; font-size: 12px;")
        layout.addWidget(self.fps_label)

        # Latency Display
        self.latency_label = QLabel("~-- ms")
        self.latency_label.setStyleSheet("color: #34d399; font-family: monospace; font-weight: 700; font-size: 12px;")
        layout.addWidget(self.latency_label)

        # Per-camera toggles. Named from the configured camera titles so the
        # operator sees "D456" / "FRONT CAM", not "main" / "pip".
        self.pip_toggle_btn = QPushButton(f"▶ {self._pip_title}")
        self.pip_toggle_btn.setObjectName("StreamToggleButton")
        self.pip_toggle_btn.setToolTip("Start / stop the inset camera on its own")
        self.pip_toggle_btn.clicked.connect(lambda: self._on_toggle_one("pip"))
        layout.addWidget(self.pip_toggle_btn)

        # Stream Toggle Button (main camera)
        self.toggle_stream_btn = QPushButton("START STREAM")
        self.toggle_stream_btn.setStyleSheet(
            "QPushButton { background-color: #8c1f38; color: white; font-weight: 700; font-size: 11px; "
            "padding: 4px 10px; border-radius: 3px; border: none; } "
            "QPushButton:hover { background-color: #6d1628; }"
        )
        self.toggle_stream_btn.clicked.connect(self._on_toggle_stream)
        layout.addWidget(self.toggle_stream_btn)

        # Fullscreen Toggle Button
        self.fullscreen_btn = QPushButton("⛶ FULLSCREEN")
        self.fullscreen_btn.setStyleSheet(
            "QPushButton { background-color: #2a1a20; color: #ddccd2; font-weight: 600; font-size: 11px; "
            "padding: 4px 10px; border-radius: 3px; border: 1px solid #4a2f39; } "
            "QPushButton:hover { background-color: #4a2f39; color: white; }"
        )
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        layout.addWidget(self.fullscreen_btn)

        return bottom_widget

    # -- Localization indicator ----------------------------------------------

    def set_localized(self, localized: Optional[bool]) -> None:
        """Green when AprilTag fixes are arriving, red when they are not.

        None means "not known yet" (no telemetry at all) and renders grey, so a
        dead link is never shown as a confident "not localized".
        """
        if localized == self._localized:
            return
        self._localized = localized
        self._render_localization_led()

    def _render_localization_led(self) -> None:
        colour, text = {
            True: ("#34d399", "● LOC"),
            False: ("#ff5f5f", "● LOC"),
        }.get(self._localized, ("#8a7079", "● LOC"))
        self.localization_led.setStyleSheet(
            f"color: {colour}; font-family: monospace; font-weight: 700; font-size: 12px;")
        self.localization_led.setText(text)

    def handle_frame(self, frame: QImage, metadata: FrameMetadata) -> None:
        """Slot for incoming decoded frames from VideoWorker."""
        self.canvas.set_frame(frame)

    # -- Picture-in-picture (secondary stream) -------------------------------

    def configure_pip(
        self,
        label: str,
        corner: str = "top_right",
        width_fraction: float = 0.26,
        enabled: bool = True,
    ) -> None:
        """Enables the inset for a second camera."""
        self.canvas.configure_pip(label, corner, width_fraction, enabled)

    def handle_pip_frame(self, frame: QImage, metadata: FrameMetadata) -> None:
        """Slot for decoded frames from the secondary VideoWorker."""
        self.canvas.set_pip_frame(frame)

    def handle_pip_state_changed(self, state: StreamState) -> None:
        """Slot for the secondary stream's state transitions."""
        self.canvas.set_pip_state(state)
        # A stream that died on its own must not leave the button reading
        # "stop", or the next press does nothing visible.
        if state in (StreamState.DISCONNECTED, StreamState.ERROR) and self._pip_running:
            self._pip_running = False
            self._render_pip_button()

    def handle_stats(self, stats: StreamStats) -> None:
        """Slot for stream statistics updates."""
        self._stats = stats
        self.fps_label.setText(stats.fps_str)
        self.latency_label.setText(stats.latency_str)
        if stats.resolution_width > 0:
            self.resolution_label.setText(f"{stats.resolution_str} HD")

    def handle_state_changed(self, state: StreamState) -> None:
        """Slot for stream state transitions."""
        self._main_state = state
        self._main_running = state in (StreamState.LIVE, StreamState.CONNECTING)
        self.state_label.setText(state.value)
        self.canvas.set_state(state, self._stats.error_message, self._stats.reconnect_count)

        if state == StreamState.LIVE:
            self.live_badge.setStyleSheet(
                "background-color: #f01f35; color: #ffffff; font-weight: 800; font-size: 11px; "
                "padding: 3px 8px; border-radius: 3px; border: 1px solid #ef4444;"
            )
            self.toggle_stream_btn.setText("STOP STREAM")
            self.toggle_stream_btn.setStyleSheet(
                "QPushButton { background-color: #c4162a; color: white; font-weight: 700; font-size: 11px; "
                "padding: 4px 10px; border-radius: 3px; border: none; } "
                "QPushButton:hover { background-color: #7f1d1d; }"
            )
        elif state == StreamState.CONNECTING:
            self.live_badge.setStyleSheet(
                "background-color: #78350f; color: #fbbf24; font-weight: 700; font-size: 11px; "
                "padding: 3px 8px; border-radius: 3px; border: 1px solid #b45309;"
            )
            self.toggle_stream_btn.setText("CONNECTING...")
        else:
            self.live_badge.setStyleSheet(
                "background-color: #2a1a20; color: #8a7079; font-weight: 700; font-size: 11px; "
                "padding: 3px 8px; border-radius: 3px; border: 1px solid #4a2f39;"
            )
            self.toggle_stream_btn.setText("START STREAM")
            self.toggle_stream_btn.setStyleSheet(
                "QPushButton { background-color: #8c1f38; color: white; font-weight: 700; font-size: 11px; "
                "padding: 4px 10px; border-radius: 3px; border: none; } "
                "QPushButton:hover { background-color: #6d1628; }"
            )
            self.fps_label.setText("-- FPS")
            self.latency_label.setText("~-- ms")

    def _on_toggle_stream(self) -> None:
        """Main camera toggle.

        Also emits the legacy broadcast signals, so anything still wired to
        "start everything" keeps working.
        """
        if self._main_state in (StreamState.LIVE, StreamState.CONNECTING):
            self._main_running = False
            self.stream_stop_one.emit("main")
            self.stream_stop_requested.emit()
        else:
            self._main_running = True
            self.stream_start_one.emit("main")
            self.stream_start_requested.emit()

    def _on_toggle_one(self, stream_id: str) -> None:
        """Toggles one camera without touching the other."""
        running = self._pip_running if stream_id == "pip" else self._main_running
        if running:
            self.stream_stop_one.emit(stream_id)
        else:
            self.stream_start_one.emit(stream_id)

        if stream_id == "pip":
            self._pip_running = not running
            self._render_pip_button()
        else:
            self._main_running = not running

    def _render_pip_button(self) -> None:
        label = f"■ {self._pip_title}" if self._pip_running else f"▶ {self._pip_title}"
        self.pip_toggle_btn.setText(label)
        self.pip_toggle_btn.setProperty("running", self._pip_running)
        self.pip_toggle_btn.style().unpolish(self.pip_toggle_btn)
        self.pip_toggle_btn.style().polish(self.pip_toggle_btn)

    def set_pip_title(self, title: str) -> None:
        """Renames the inset and its toggle button."""
        self._pip_title = title
        self.canvas.configure_pip(
            title,
            self.canvas._pip_corner,
            self.canvas._pip_width_fraction,
            self.canvas.pip_enabled,
        )
        self._render_pip_button()

    def toggle_fullscreen(self) -> None:
        """Toggles fullscreen display mode for the video player.

        The widget has to leave its parent layout *before* it becomes a
        top-level window. Flipping the window flags while the QSplitter still
        holds it leaves the splitter geometry-managing a widget that is now a
        window, and the two fight each other in a relayout loop that freezes
        the whole UI. Detaching first, and re-inserting at the same index on
        the way back, keeps the sidebar proportions the operator had set.
        """
        if not self._is_fullscreen:
            parent = self.parentWidget()
            self._restore_parent = parent
            self._restore_splitter = None
            self._restore_index = -1
            self._restore_sizes = None
            if isinstance(parent, QSplitter):
                self._restore_splitter = parent
                self._restore_index = parent.indexOf(self)
                self._restore_sizes = parent.sizes()

            self.setParent(None)
            self.setWindowFlags(Qt.Window)
            self.showFullScreen()
            self._is_fullscreen = True
            self.fullscreen_btn.setText("✕ EXIT FULLSCREEN")
        else:
            self.setWindowFlags(Qt.Widget)
            splitter = self._restore_splitter
            if splitter is not None:
                splitter.insertWidget(self._restore_index, self)
                if self._restore_sizes:
                    splitter.setSizes(self._restore_sizes)
            elif self._restore_parent is not None:
                self.setParent(self._restore_parent)
            # setWindowFlags() hides the widget, so it must be shown again
            # explicitly -- showNormal() alone leaves it invisible once it has
            # been re-parented into a layout.
            self.show()
            self._is_fullscreen = False
            self.fullscreen_btn.setText("⛶ FULLSCREEN")
        self.fullscreen_toggled.emit(self._is_fullscreen)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Allows ESC key to exit fullscreen mode."""
        if event.key() == Qt.Key_Escape and self._is_fullscreen:
            self.toggle_fullscreen()
        else:
            super().keyPressEvent(event)
