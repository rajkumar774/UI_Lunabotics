"""Status Card UI Component."""

from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QHBoxLayout, QWidget


class StatusCard(QFrame):
    """Reusable status panel container with structured header and body."""

    def __init__(
        self,
        title: str,
        initial_value: str = "--",
        subtext: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setProperty("class", "StatusCard")
        self.setFrameShape(QFrame.StyledPanel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        # Header Title
        self.header_label = QLabel(title.upper())
        self.header_label.setObjectName("CardHeader")
        layout.addWidget(self.header_label)

        # Main Value / Status
        self.value_label = QLabel(initial_value)
        self.value_label.setObjectName("CardValue")
        self.value_label.setWordWrap(True)
        layout.addWidget(self.value_label)

        # Subtext / Metadata
        self.subtext_label = QLabel(subtext)
        self.subtext_label.setObjectName("CardSubtext")
        if not subtext:
            self.subtext_label.hide()
        layout.addWidget(self.subtext_label)

        layout.addStretch()

    def set_value(self, text: str) -> None:
        """Updates the primary value display."""
        self.value_label.setText(text)

    def set_subtext(self, text: str) -> None:
        """Updates the secondary subtext display."""
        self.subtext_label.setText(text)
        self.subtext_label.setVisible(bool(text))
