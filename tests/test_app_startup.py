"""Tests for application startup, imports, and UI layout initialization."""

import pytest
from PySide6.QtWidgets import QApplication

from mission_control.core.app import MissionControlApp
from mission_control.core.config import AppConfig, RoverConfig
from mission_control.comms.mock_client import MockCommsClient
from mission_control.ui.main_window import MainWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_app_initialization(qapp):
    """Verifies that MissionControlApp initializes cleanly in standalone mode."""
    app = MissionControlApp([])
    initialized = app.initialize()

    assert initialized is True
    assert app.app_config is not None
    assert app.rover_config is not None
    assert app.comms_client is not None
    assert app.main_window is not None

    # Clean shutdown
    app.cleanup()


def test_main_window_structure(qapp):
    """Verifies that MainWindow contains the expected mission control placeholders."""
    app_cfg = AppConfig()
    rover_cfg = RoverConfig()
    client = MockCommsClient()
    client.start()

    window = MainWindow(app_cfg, rover_cfg, client)

    assert window.windowTitle() == app_cfg.ui.window_title
    assert window.mission_status.clock_label.text() == "25:00"
    # Telemetry strip starts in its safe no-data state until snapshots arrive.
    assert window.telemetry_panel.power.charge_row.value() == "--"

    # The ROVER LINK badge, CONNECT MOCK and E-STOP were removed from the
    # header. What must survive is the behaviour behind them: on a dropped
    # link the panels fall back to NO DATA instead of leaving stale values
    # on screen, where an operator would read them as current.
    for removed in ("connection_badge", "connect_btn", "estop_btn", "safety_badge"):
        assert not hasattr(window, removed), f"{removed} should be gone"

    client.connect_rover()
    client.tick_telemetry(0.1)
    window._on_connection_changed("NOT CONNECTED")
    assert window.telemetry_panel.power.charge_row.value() == "--"

    client.stop()
    window.close()
