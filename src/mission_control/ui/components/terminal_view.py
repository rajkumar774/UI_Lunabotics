"""VT100 terminal rendering surface.

Backed by pyte, which owns the screen model: bytes arriving from the remote PTY
are fed into a pyte Stream, and this widget only paints the resulting character
grid. That split is what makes `ros2 launch` output, progress bars that rewrite
their line, and full-screen curses programs all render correctly without this
module knowing anything about escape sequences.

Painting batches each row into runs of identical styling rather than drawing
cell by cell, because a per-cell drawText on an 80x30 grid is slow enough to be
visible when a node is logging at speed.
"""

import codecs
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QKeyEvent,
    QPainter,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from mission_control.comms.jetson_ssh import IDLE_SENTINEL

#: Scrollback retained above the visible screen.
HISTORY_LINES = 2000

DEFAULT_COLS = 80
DEFAULT_ROWS = 24

BACKGROUND = QColor("#0d0709")
FOREGROUND = QColor("#e8dcdf")
CURSOR_COLOR = QColor("#e0657f")

#: pyte reports colours as names (or as 6-digit hex for 256/true-colour).
ANSI_COLORS = {
    "black": "#231619",
    "red": "#f87171",
    "green": "#4ade80",
    "brown": "#fbbf24",
    "yellow": "#fbbf24",
    "blue": "#60a5fa",
    "magenta": "#e879f9",
    "cyan": "#38bdf8",
    "white": "#e8dcdf",
    "brightblack": "#8a7079",
    "brightred": "#fca5a5",
    "brightgreen": "#86efac",
    "brightbrown": "#fcd34d",
    "brightyellow": "#fcd34d",
    "brightblue": "#93c5fd",
    "brightmagenta": "#f0abfc",
    "brightcyan": "#7dd3fc",
    "brightwhite": "#fdf7f8",
}


def resolve_color(name: str, default: QColor) -> QColor:
    """Maps a pyte colour token to a QColor.

    Handles the named 16-colour palette, bare 6-digit hex from 256-colour and
    true-colour sequences, and the 'default' sentinel.
    """
    if not name or name == "default":
        return default
    mapped = ANSI_COLORS.get(name)
    if mapped is not None:
        return QColor(mapped)
    if len(name) == 6:
        try:
            return QColor(f"#{name}")
        except Exception:
            return default
    color = QColor(name)
    return color if color.isValid() else default


def _make_screen_class():
    """Builds a pyte screen that reports the shell's idle sentinel.

    The remote shell is configured with a PROMPT_COMMAND that emits an OSC 0
    title sequence before every prompt. OSC 0 is consumed by the emulator and
    never printed, so this gives an exact "the shell is back at a prompt"
    signal without polluting the screen or guessing at prompt text.

    Defined lazily so importing this module does not require pyte at import
    time for callers that never construct a terminal.
    """
    import pyte

    class _SentinelScreen(pyte.HistoryScreen):
        def __init__(self, *args, **kwargs):
            self.on_idle = None
            super().__init__(*args, **kwargs)

        def set_title(self, param):
            super().set_title(param)
            if param == IDLE_SENTINEL and self.on_idle is not None:
                self.on_idle()

    return _SentinelScreen


class TerminalView(QWidget):
    """Paints a pyte screen and converts key presses into PTY bytes."""

    #: Emitted when the grid geometry changes, so the PTY can be resized to match.
    resized = Signal(int, int)       # (cols, rows)
    #: Emitted with the bytes a key press should write to the remote PTY.
    key_pressed = Signal(bytes)
    #: Emitted when the remote shell returns to a prompt (see _make_screen_class).
    became_idle = Signal()

    def __init__(self, parent: Optional[QWidget] = None, font_point_size: int = 10):
        super().__init__(parent)
        import pyte

        self.setObjectName("TerminalView")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.IBeamCursor)

        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setPointSize(font_point_size)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)

        metrics = QFontMetricsF(font)
        self._cell_w = max(1.0, metrics.horizontalAdvance("M"))
        self._cell_h = max(1.0, metrics.height())
        self._ascent = metrics.ascent()

        self._cols = DEFAULT_COLS
        self._rows = DEFAULT_ROWS
        screen_class = _make_screen_class()
        self.screen = screen_class(self._cols, self._rows, history=HISTORY_LINES, ratio=0.5)
        self.screen.on_idle = self._on_shell_idle
        self.stream = pyte.Stream(self.screen)

        # Bytes can split a multi-byte UTF-8 sequence across reads.
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._scroll_offset = 0
        # A shell is busy from the moment a command is submitted until it next
        # reaches a prompt. Starts busy: nothing has reported a prompt yet.
        self._busy = True

    # -- Geometry ------------------------------------------------------------

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    def sizeHint(self) -> QSize:
        return QSize(int(self._cell_w * DEFAULT_COLS), int(self._cell_h * DEFAULT_ROWS))

    def minimumSizeHint(self) -> QSize:
        return QSize(int(self._cell_w * 40), int(self._cell_h * 6))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cols = max(20, int(self.width() // self._cell_w))
        rows = max(5, int(self.height() // self._cell_h))
        if (cols, rows) == (self._cols, self._rows):
            return
        self._cols, self._rows = cols, rows
        self.screen.resize(rows, cols)
        self.resized.emit(cols, rows)

    # -- Input ---------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        """Feeds raw PTY bytes into the screen model and repaints."""
        text = self._decoder.decode(data)
        if not text:
            return
        # New output cancels a scrollback view, matching every other terminal.
        if self._scroll_offset:
            self._scroll_to_bottom()
        self.stream.feed(text)
        self.update()

    def write_local(self, text: str) -> None:
        """Writes text directly to the screen without going through the PTY.

        Used for status lines the UI itself produces (connecting, errors), so
        they appear inline with remote output.
        """
        self.stream.feed(text)
        self.update()

    def clear(self) -> None:
        """Resets the screen and scrollback."""
        self.screen.reset()
        self.screen.resize(self._rows, self._cols)
        self._scroll_offset = 0
        self.update()

    # -- Busy / idle tracking ------------------------------------------------

    @property
    def is_busy(self) -> bool:
        """True while a command is running in this shell.

        Driven by the shell's own prompt, not by a timer or a guess at the
        prompt's text, so it stays correct for a command that produces no
        output for minutes.
        """
        return self._busy

    def mark_command_submitted(self) -> None:
        """Marks the shell busy; cleared when it next reaches a prompt."""
        self._busy = True

    def _on_shell_idle(self) -> None:
        was_busy = self._busy
        self._busy = False
        if was_busy:
            self.became_idle.emit()

    def visible_text(self) -> str:
        """The current screen as plain text, with trailing blanks stripped."""
        return "\n".join(line.rstrip() for line in self.screen.display).rstrip()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        data = self._key_to_bytes(event)
        if data is None:
            super().keyPressEvent(event)
            return
        if data:
            # Enter submits whatever is on the line, so assume busy until the
            # shell reports a prompt again.
            if data == b"\r":
                self.mark_command_submitted()
            self.key_pressed.emit(data)
        event.accept()

    def _key_to_bytes(self, event: QKeyEvent) -> Optional[bytes]:
        """Translates a key press into PTY bytes, or None to let Qt handle it."""
        key = event.key()
        modifiers = event.modifiers()
        ctrl = bool(modifiers & Qt.ControlModifier)
        shift = bool(modifiers & Qt.ShiftModifier)

        # Scrollback navigation is local to the widget; it never reaches the PTY.
        if shift and key == Qt.Key_PageUp:
            self._scroll_pages(-1)
            return b""
        if shift and key == Qt.Key_PageDown:
            self._scroll_pages(1)
            return b""

        if ctrl and shift and key == Qt.Key_C:
            self._copy_to_clipboard()
            return b""
        if ctrl and shift and key == Qt.Key_V:
            return self._clipboard_bytes()

        special = {
            Qt.Key_Return: b"\r",
            Qt.Key_Enter: b"\r",
            Qt.Key_Backspace: b"\x7f",
            Qt.Key_Tab: b"\t",
            Qt.Key_Escape: b"\x1b",
            Qt.Key_Up: b"\x1b[A",
            Qt.Key_Down: b"\x1b[B",
            Qt.Key_Right: b"\x1b[C",
            Qt.Key_Left: b"\x1b[D",
            Qt.Key_Home: b"\x1b[H",
            Qt.Key_End: b"\x1b[F",
            Qt.Key_Delete: b"\x1b[3~",
            Qt.Key_PageUp: b"\x1b[5~",
            Qt.Key_PageDown: b"\x1b[6~",
            Qt.Key_Insert: b"\x1b[2~",
        }
        if key in special:
            return special[key]

        if ctrl and Qt.Key_A <= key <= Qt.Key_Z:
            # Ctrl-A..Ctrl-Z map onto control codes 0x01..0x1a.
            return bytes([key - Qt.Key_A + 1])
        if ctrl and key == Qt.Key_BracketLeft:
            return b"\x1b"
        if ctrl and key == Qt.Key_Backslash:
            return b"\x1c"
        if ctrl and key == Qt.Key_Space:
            return b"\x00"

        text = event.text()
        if text:
            return text.encode("utf-8", errors="replace")
        return None

    def _copy_to_clipboard(self) -> None:
        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.visible_text())

    def _clipboard_bytes(self) -> bytes:
        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is None:
            return b""
        return clipboard.text().encode("utf-8", errors="replace")

    # -- Scrollback ----------------------------------------------------------

    def wheelEvent(self, event) -> None:
        # Wheel up (positive delta) scrolls back into history, hence the negation.
        steps = int(event.angleDelta().y() / 120.0)
        if steps:
            self._scroll_lines(-steps * 3)
        event.accept()

    def _scroll_lines(self, delta: int) -> None:
        """Moves the scrollback view; negative scrolls up into history."""
        if delta < 0:
            for _ in range(-delta):
                if not self.screen.history.top:
                    break
                self.screen.prev_page()
                self._scroll_offset += 1
        else:
            for _ in range(delta):
                if self._scroll_offset <= 0:
                    break
                self.screen.next_page()
                self._scroll_offset -= 1
        self.update()

    def _scroll_pages(self, pages: int) -> None:
        self._scroll_lines(pages * max(1, self._rows - 1))

    def _scroll_to_bottom(self) -> None:
        while self._scroll_offset > 0:
            self.screen.next_page()
            self._scroll_offset -= 1

    @property
    def scroll_offset(self) -> int:
        """Lines currently scrolled up into history; 0 means live view."""
        return self._scroll_offset

    # -- Painting ------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.fillRect(self.rect(), BACKGROUND)

        buffer = self.screen.buffer
        for row in range(self._rows):
            line = buffer[row]
            for start, end, char in self._style_runs(line):
                self._paint_run(painter, line, row, start, end, char)

        self._paint_cursor(painter)
        painter.end()

    def _style_runs(self, line) -> List[Tuple[int, int, object]]:
        """Groups a row into [start, end) spans that share one style.

        Returns the representative Char for each span; the text is re-read from
        the line when painting so only styling is compared here.
        """
        runs: List[Tuple[int, int, object]] = []
        start = 0
        previous = None

        for col in range(self._cols):
            char = line[col]
            key = (char.fg, char.bg, char.bold, char.italics, char.underscore, char.reverse)
            if previous is None:
                previous, start = key, col
                continue
            if key != previous:
                runs.append((start, col, line[start]))
                previous, start = key, col

        if previous is not None:
            runs.append((start, self._cols, line[start]))
        return runs

    def _paint_run(self, painter: QPainter, line, row: int, start: int, end: int, char) -> None:
        text = "".join(line[c].data for c in range(start, end))
        y = row * self._cell_h

        fg = resolve_color(char.fg, FOREGROUND)
        bg = resolve_color(char.bg, BACKGROUND)
        if char.reverse:
            fg, bg = bg, fg
        if char.bold and char.fg == "default":
            fg = FOREGROUND.lighter(115)

        x = start * self._cell_w
        width = (end - start) * self._cell_w

        if bg != BACKGROUND:
            painter.fillRect(int(x), int(y), int(width) + 1, int(self._cell_h) + 1, bg)

        if not text.strip():
            return

        font = painter.font()
        if font.bold() != bool(char.bold) or font.italic() != bool(char.italics):
            font.setBold(bool(char.bold))
            font.setItalic(bool(char.italics))
            painter.setFont(font)

        painter.setPen(fg)
        painter.drawText(int(x), int(y + self._ascent), text)

        if char.underscore:
            underline_y = int(y + self._cell_h - 1)
            painter.drawLine(int(x), underline_y, int(x + width), underline_y)

    def _paint_cursor(self, painter: QPainter) -> None:
        """Draws the cursor as a block, hidden while scrolled into history."""
        cursor = self.screen.cursor
        if cursor.hidden or self._scroll_offset > 0 or not self.hasFocus():
            return
        if not (0 <= cursor.y < self._rows and 0 <= cursor.x < self._cols):
            return

        x = cursor.x * self._cell_w
        y = cursor.y * self._cell_h
        painter.fillRect(int(x), int(y), int(self._cell_w), int(self._cell_h), CURSOR_COLOR)

        char = self.screen.buffer[cursor.y][cursor.x]
        if char.data.strip():
            painter.setPen(BACKGROUND)
            painter.drawText(int(x), int(y + self._ascent), char.data)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()
