"""Records each competition run and writes it to its own folder.

A run is worth 25 minutes of a team's year, and the operator will not remember
what the battery did at minute 18. This writes that down: a timeline of
telemetry, the commands sent, and the alerts the rover raised, in a folder
stamped with the run's start time.

Design notes:

* Samples are throttled, not taken at the UI refresh rate. The telemetry bridge
  publishes ~30 snapshots a second; at 25 minutes that is 45,000 rows nobody
  will read. 2 Hz gives a 3,000-row file that still resolves a voltage sag.
* Nothing is written until the run ends, so a run in progress costs one list
  append per sample and no disk I/O competing with the video decode.
* A failed write must never take the UI down with it -- `finish()` returns the
  path on success and None on failure, and the caller decides what to say.
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from mission_control.telemetry.state import RoverState

logger = logging.getLogger(__name__)

#: Samples per second written to the CSV. See the module docstring.
DEFAULT_SAMPLE_HZ = 2.0


@dataclass
class _Sample:
    t: float                     # seconds since the run started
    battery_pct: float
    voltage: float
    current_a: float
    power_w: Optional[float]
    energy_wh: Optional[float]
    speed: float
    turn_rate: float
    roll: float
    pitch: float
    yaw: float
    connection: str
    localized: bool
    estopped: bool


@dataclass
class _Event:
    t: float
    kind: str                    # COMMAND | ALERT | PHASE
    text: str


@dataclass
class RunLogger:
    """Collects one run, then writes it to `output_dir/run_<timestamp>/`."""

    output_dir: Path
    sample_hz: float = DEFAULT_SAMPLE_HZ
    #: Injectable so the throttle can be tested deterministically instead of
    #: racing the real clock.
    time_source: Callable[[], float] = time.monotonic

    _started_at: Optional[float] = field(default=None, init=False)
    _started_wall: Optional[datetime] = field(default=None, init=False)
    _samples: List[_Sample] = field(default_factory=list, init=False)
    _events: List[_Event] = field(default_factory=list, init=False)
    _last_sample_t: float = field(default=0.0, init=False)
    _seen_alerts: set = field(default_factory=set, init=False)

    # -- lifecycle ----------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._started_at is not None

    def start(self) -> None:
        """Begins a run. Starting twice keeps the FIRST start time.

        The operator may pause and resume the clock mid-run; that is one run,
        not several, so a second start must not silently reset the timeline.
        """
        if self.is_recording:
            return
        self._started_at = self.time_source()
        self._started_wall = datetime.now()
        self._samples.clear()
        self._events.clear()
        self._seen_alerts.clear()
        self._last_sample_t = -1.0
        logger.info("Run recording started.")

    def discard(self) -> None:
        """Throws the run away (operator pressed reset)."""
        self._started_at = None
        self._started_wall = None
        self._samples.clear()
        self._events.clear()

    # -- collection ---------------------------------------------------------

    def record_state(self, state: RoverState) -> None:
        """Takes a telemetry sample, throttled to sample_hz."""
        if not self.is_recording or not isinstance(state, RoverState):
            return
        now = self.time_source() - self._started_at
        if now - self._last_sample_t < (1.0 / max(0.1, self.sample_hz)):
            return
        self._last_sample_t = now

        b, imu, drive = state.battery, state.imu, state.drive
        self._samples.append(_Sample(
            t=now, battery_pct=b.percentage, voltage=b.voltage,
            current_a=b.current_amps, power_w=b.power_watts,
            energy_wh=b.energy_wh, speed=drive.linear_velocity,
            turn_rate=drive.angular_velocity, roll=imu.roll_deg,
            pitch=imu.pitch_deg, yaw=imu.yaw_deg,
            connection=str(getattr(state.connection_state, "value", "")),
            localized=state.is_localized(), estopped=state.is_estopped,
        ))

        # Alerts are deduplicated: the rover republishes the same DiagnosticArray
        # continuously, and 3,000 copies of one warning hides everything else.
        for alert in state.diagnostics.active_alerts:
            if alert not in self._seen_alerts:
                self._seen_alerts.add(alert)
                self._events.append(_Event(now, "ALERT", alert))

    def record_command(self, name: str, ok: bool) -> None:
        if not self.is_recording:
            return
        t = self.time_source() - self._started_at
        self._events.append(_Event(t, "COMMAND", f"{name} -> {'OK' if ok else 'FAILED'}"))

    def record_event(self, text: str) -> None:
        if not self.is_recording:
            return
        self._events.append(_Event(self.time_source() - self._started_at, "PHASE", text))

    # -- output -------------------------------------------------------------

    def finish(self, reason: str = "run complete") -> Optional[Path]:
        """Writes the run and returns its folder, or None if nothing was written.

        Never raises: a disk problem at the end of a run must not take the UI
        down with it, and the operator still has the screen in front of them.
        """
        if not self.is_recording:
            return None
        started_wall = self._started_wall
        samples, events = list(self._samples), list(self._events)
        duration = self.time_source() - self._started_at
        self._started_at = None

        if not samples:
            logger.warning("Run finished with no telemetry samples; nothing written.")
            return None

        stamp = started_wall.strftime("%Y%m%d-%H%M%S")
        folder = Path(self.output_dir) / f"run_{stamp}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self._write_csv(folder / "telemetry.csv", samples)
            self._write_events(folder / "events.log", events)
            self._write_summary(folder / "summary.txt", started_wall, duration,
                                samples, events, reason)
        except OSError as exc:
            logger.error(f"Could not write run log to {folder}: {exc}")
            return None

        logger.info(f"Run log written to {folder}")
        return folder

    @staticmethod
    def _write_csv(path: Path, samples: List[_Sample]) -> None:
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "t_seconds", "battery_pct", "voltage_v", "current_a", "power_w",
                "energy_wh", "speed_mps", "turn_rate_rads", "roll_deg",
                "pitch_deg", "yaw_deg", "connection", "localized", "estopped",
            ])
            for s in samples:
                writer.writerow([
                    f"{s.t:.2f}", f"{s.battery_pct:.1f}", f"{s.voltage:.2f}",
                    f"{s.current_a:.2f}",
                    "" if s.power_w is None else f"{s.power_w:.1f}",
                    "" if s.energy_wh is None else f"{s.energy_wh:.0f}",
                    f"{s.speed:.3f}", f"{s.turn_rate:.3f}", f"{s.roll:.1f}",
                    f"{s.pitch:.1f}", f"{s.yaw:.1f}", s.connection,
                    int(s.localized), int(s.estopped),
                ])

    @staticmethod
    def _write_events(path: Path, events: List[_Event]) -> None:
        with path.open("w") as handle:
            if not events:
                handle.write("(no commands or alerts during this run)\n")
                return
            for e in events:
                handle.write(f"{e.t:8.1f}s  {e.kind:<8} {e.text}\n")

    @staticmethod
    def _write_summary(path: Path, started: datetime, duration: float,
                       samples: List[_Sample], events: List[_Event],
                       reason: str) -> None:
        first, last = samples[0], samples[-1]
        voltages = [s.voltage for s in samples if s.voltage > 0]
        draws = [s.current_a for s in samples]
        speeds = [abs(s.speed) for s in samples]
        energies = [s.energy_wh for s in samples if s.energy_wh is not None]
        localized = sum(1 for s in samples if s.localized)

        def line(label, value):
            return f"{label:<26}{value}\n"

        with path.open("w") as handle:
            handle.write("AGALVAAN RUN SUMMARY\n")
            handle.write("=" * 52 + "\n\n")
            handle.write(line("Started", started.strftime("%Y-%m-%d %H:%M:%S")))
            handle.write(line("Duration", f"{int(duration // 60)}m {int(duration % 60)}s"))
            handle.write(line("Ended because", reason))
            handle.write(line("Telemetry samples", len(samples)))
            handle.write("\nPOWER\n" + "-" * 52 + "\n")
            handle.write(line("Battery start -> end", f"{first.battery_pct:.0f}% -> {last.battery_pct:.0f}%"))
            handle.write(line("Voltage start -> end", f"{first.voltage:.2f} V -> {last.voltage:.2f} V"))
            if voltages:
                handle.write(line("Lowest voltage", f"{min(voltages):.2f} V"))
            if draws:
                handle.write(line("Peak draw", f"{max(draws):.2f} A"))
            if len(energies) >= 2:
                handle.write(line("Energy used", f"{energies[-1] - energies[0]:.0f} Wh"))
            handle.write("\nMOTION\n" + "-" * 52 + "\n")
            handle.write(line("Top speed", f"{max(speeds):.2f} m/s" if speeds else "--"))
            handle.write("\nLOCALIZATION\n" + "-" * 52 + "\n")
            pct = (localized / len(samples)) * 100.0
            handle.write(line("Samples with a tag fix", f"{localized} of {len(samples)} ({pct:.0f}%)"))
            handle.write("\nEVENTS\n" + "-" * 52 + "\n")
            commands = [e for e in events if e.kind == "COMMAND"]
            alerts = [e for e in events if e.kind == "ALERT"]
            handle.write(line("Commands sent", len(commands)))
            handle.write(line("Distinct alerts", len(alerts)))
            if alerts:
                handle.write("\nAlerts raised (first time seen):\n")
                for a in alerts:
                    handle.write(f"  {a.t:7.1f}s  {a.text}\n")
            handle.write("\nFiles: telemetry.csv (timeline), events.log (commands + alerts)\n")
