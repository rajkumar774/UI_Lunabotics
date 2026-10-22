#!/usr/bin/env python3
"""Generates the plain-language 'How Mission Control talks to the rover' PDF.

Aimed at someone who has never seen the system: a new team member, a judge, a
faculty advisor. Pictures first, words second, no jargon that is not explained
on the same page.

All artwork is drawn with vector primitives, so there are no image files to
lose and the document stays a few kilobytes.

    python3 tools/generate_how_it_works.py
"""

from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from reportlab.graphics.shapes import (  # noqa: E402
    Circle, Drawing, Group, Line, PolyLine, Polygon, Rect, String, Wedge,
)
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import LETTER  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from mission_control.core.config import load_config  # noqa: E402

BLUE = colors.HexColor("#1f6fb2")
DARK = colors.HexColor("#22303f")
GREY = colors.HexColor("#8794a3")
LIGHT = colors.HexColor("#dde6ef")
GREEN = colors.HexColor("#2d8a55")
AMBER = colors.HexColor("#d08700")
RED = colors.HexColor("#c0392b")
PAPER = colors.HexColor("#f4f7fa")
WHITE = colors.white

base = getSampleStyleSheet()
ST = {
    "title": ParagraphStyle("t", parent=base["Title"], fontName="Helvetica-Bold",
                            fontSize=22, leading=26, textColor=BLUE, spaceAfter=2),
    "sub": ParagraphStyle("s", parent=base["Normal"], fontName="Helvetica",
                          fontSize=10, leading=14, textColor=GREY, spaceAfter=12),
    "h": ParagraphStyle("h", parent=base["Heading2"], fontName="Helvetica-Bold",
                        fontSize=15, leading=18, textColor=BLUE,
                        spaceBefore=10, spaceAfter=6),
    "p": ParagraphStyle("p", parent=base["Normal"], fontName="Helvetica",
                        fontSize=10.5, leading=15, textColor=DARK, spaceAfter=7),
    "big": ParagraphStyle("b", parent=base["Normal"], fontName="Helvetica-Bold",
                          fontSize=12, leading=16, textColor=DARK, spaceAfter=6),
    "note": ParagraphStyle("n", parent=base["Normal"], fontName="Helvetica-Oblique",
                           fontSize=9.5, leading=13, textColor=GREY, spaceAfter=6),
}


def P(t, k="p"):
    return Paragraph(t, ST[k])


# --------------------------------------------------------------- clip art

def laptop(x, y, s=1.0, label="LAPTOP"):
    """Operator laptop: screen plus a base."""
    g = Group()
    g.add(Rect(x, y + 10 * s, 46 * s, 30 * s, rx=2, ry=2,
               fillColor=DARK, strokeColor=DARK))
    g.add(Rect(x + 3 * s, y + 13 * s, 40 * s, 24 * s,
               fillColor=BLUE, strokeColor=None))
    # a couple of "windows" on the screen
    g.add(Rect(x + 6 * s, y + 26 * s, 16 * s, 8 * s, fillColor=WHITE, strokeColor=None))
    g.add(Rect(x + 24 * s, y + 26 * s, 16 * s, 8 * s, fillColor=LIGHT, strokeColor=None))
    g.add(Rect(x + 6 * s, y + 16 * s, 34 * s, 7 * s, fillColor=LIGHT, strokeColor=None))
    g.add(Polygon([x - 4 * s, y, x + 50 * s, y, x + 46 * s, y + 10 * s, x, y + 10 * s],
                  fillColor=GREY, strokeColor=GREY))
    g.add(String(x + 23 * s, y - 17 * s, label, fontName="Helvetica-Bold",
                 fontSize=7.5 * s, fillColor=DARK, textAnchor="middle"))
    return g


def router(x, y, s=1.0, label="Wi-Fi ROUTER"):
    """Team WAP: a box with two antennas and radio waves."""
    g = Group()
    g.add(Rect(x, y, 44 * s, 15 * s, rx=3, ry=3, fillColor=DARK, strokeColor=DARK))
    for dx in (10, 22, 34):
        g.add(Circle(x + dx * s, y + 7.5 * s, 2 * s, fillColor=GREEN, strokeColor=None))
    g.add(Line(x + 8 * s, y + 15 * s, x + 2 * s, y + 30 * s, strokeColor=DARK, strokeWidth=1.6 * s))
    g.add(Line(x + 36 * s, y + 15 * s, x + 42 * s, y + 30 * s, strokeColor=DARK, strokeWidth=1.6 * s))
    for r in (9, 14, 19):
        g.add(Wedge(x + 22 * s, y + 30 * s, r * s, 35, 145,
                    strokeColor=BLUE, fillColor=None, strokeWidth=1.1 * s))
    g.add(String(x + 22 * s, y - 17 * s, label, fontName="Helvetica-Bold",
                 fontSize=7.5 * s, fillColor=DARK, textAnchor="middle"))
    return g


def rover(x, y, s=1.0, label="ROVER"):
    """Rover: body, wheels, mast with LiDAR, and a bucket."""
    g = Group()
    g.add(Rect(x + 6 * s, y + 9 * s, 46 * s, 18 * s, rx=3, ry=3,
               fillColor=BLUE, strokeColor=BLUE))
    for cx in (15, 29, 43):
        g.add(Circle(x + cx * s, y + 7 * s, 7 * s, fillColor=DARK, strokeColor=DARK))
        g.add(Circle(x + cx * s, y + 7 * s, 2.6 * s, fillColor=GREY, strokeColor=None))
    # mast + LiDAR puck
    g.add(Rect(x + 27 * s, y + 27 * s, 3 * s, 12 * s, fillColor=GREY, strokeColor=None))
    g.add(Circle(x + 28.5 * s, y + 41 * s, 4.5 * s, fillColor=AMBER, strokeColor=DARK))
    # bucket at the front
    g.add(Polygon([x + 52 * s, y + 12 * s, x + 64 * s, y + 12 * s,
                   x + 61 * s, y + 24 * s, x + 52 * s, y + 24 * s],
                  fillColor=GREY, strokeColor=DARK))
    g.add(String(x + 32 * s, y - 14 * s, label, fontName="Helvetica-Bold",
                 fontSize=7.5 * s, fillColor=DARK, textAnchor="middle"))
    return g


def chip(x, y, s=1.0, label="ESP32"):
    """Small controller board with pins."""
    g = Group()
    g.add(Rect(x, y, 26 * s, 18 * s, rx=2, ry=2, fillColor=DARK, strokeColor=DARK))
    g.add(Rect(x + 5 * s, y + 4 * s, 16 * s, 10 * s, fillColor=GREY, strokeColor=None))
    for i in range(5):
        g.add(Line(x + 4 * s + i * 4.4 * s, y, x + 4 * s + i * 4.4 * s, y - 3.5 * s,
                   strokeColor=AMBER, strokeWidth=1.3 * s))
    g.add(String(x + 13 * s, y - 15 * s, label, fontName="Helvetica-Bold",
                 fontSize=7 * s, fillColor=DARK, textAnchor="middle"))
    return g


def gamepad(x, y, s=1.0):
    g = Group()
    g.add(Rect(x, y, 34 * s, 16 * s, rx=7, ry=7, fillColor=DARK, strokeColor=DARK))
    g.add(Circle(x + 9 * s, y + 8 * s, 3.4 * s, fillColor=LIGHT, strokeColor=None))
    g.add(Circle(x + 25 * s, y + 10 * s, 2.3 * s, fillColor=GREEN, strokeColor=None))
    g.add(Circle(x + 25 * s, y + 5 * s, 2.3 * s, fillColor=AMBER, strokeColor=None))
    return g


def camera(x, y, s=1.0):
    g = Group()
    g.add(Rect(x, y, 26 * s, 17 * s, rx=2, ry=2, fillColor=DARK, strokeColor=DARK))
    g.add(Circle(x + 13 * s, y + 8.5 * s, 6 * s, fillColor=BLUE, strokeColor=LIGHT))
    g.add(Circle(x + 13 * s, y + 8.5 * s, 2.4 * s, fillColor=WHITE, strokeColor=None))
    g.add(Rect(x + 18 * s, y + 14 * s, 6 * s, 4 * s, fillColor=GREY, strokeColor=None))
    return g


def gauge(x, y, s=1.0):
    """Telemetry dial."""
    g = Group()
    g.add(Circle(x + 11 * s, y + 11 * s, 11 * s, fillColor=WHITE, strokeColor=DARK, strokeWidth=1.3 * s))
    g.add(Wedge(x + 11 * s, y + 11 * s, 11 * s, 180, 300, fillColor=GREEN, strokeColor=None))
    g.add(Wedge(x + 11 * s, y + 11 * s, 11 * s, 300, 360, fillColor=AMBER, strokeColor=None))
    g.add(Circle(x + 11 * s, y + 11 * s, 7.5 * s, fillColor=WHITE, strokeColor=None))
    g.add(Line(x + 11 * s, y + 11 * s, x + 4 * s, y + 16 * s, strokeColor=RED, strokeWidth=1.6 * s))
    g.add(Circle(x + 11 * s, y + 11 * s, 1.6 * s, fillColor=DARK, strokeColor=None))
    return g


def terminal(x, y, s=1.0):
    g = Group()
    g.add(Rect(x, y, 30 * s, 20 * s, rx=2, ry=2, fillColor=DARK, strokeColor=DARK))
    g.add(String(x + 4 * s, y + 11 * s, "$ _", fontName="Courier-Bold",
                 fontSize=8 * s, fillColor=GREEN))
    g.add(Line(x + 4 * s, y + 7 * s, x + 20 * s, y + 7 * s, strokeColor=GREY, strokeWidth=0.8 * s))
    return g


def arrow(x1, y1, x2, y2, colour=BLUE, w=2.0, dash=False, head=4.5):
    g = Group()
    line = Line(x1, y1, x2, y2, strokeColor=colour, strokeWidth=w)
    if dash:
        line.strokeDashArray = [4, 3]
    g.add(line)
    import math
    a = math.atan2(y2 - y1, x2 - x1)
    g.add(Polygon([
        x2, y2,
        x2 - head * math.cos(a - 0.45), y2 - head * math.sin(a - 0.45),
        x2 - head * math.cos(a + 0.45), y2 - head * math.sin(a + 0.45),
    ], fillColor=colour, strokeColor=colour))
    return g


def caption(x, y, text, size=8, colour=GREY, bold=False, anchor="middle"):
    return String(x, y, text, fontSize=size, textAnchor=anchor,
                  fillColor=colour,
                  fontName="Helvetica-Bold" if bold else "Helvetica")


# ------------------------------------------------------------- the pages

def page_overview():
    d = Drawing(460, 252)
    d.add(Rect(0, 0, 460, 252, fillColor=PAPER, strokeColor=LIGHT))

    ROW = 168                      # baseline for the three main icons
    LBL = ROW - 2                  # icon labels sit well clear of the artwork

    # Laptop label goes ABOVE: the joystick arrow comes up from below and
    # would otherwise run straight through it.
    d.add(laptop(22, ROW, 1.0, label=""))
    d.add(caption(45, ROW + 46, "LAPTOP", 8.5, DARK, True))
    d.add(caption(45, ROW + 36, "you sit here", 7.5))

    d.add(router(175, ROW - 2, 1.0))
    d.add(caption(197, LBL - 28, "stands in the arena", 7.5))

    d.add(rover(330, ROW - 4, 1.0))
    d.add(caption(362, LBL - 32, "out on the regolith", 7.5))

    # laptop -> router: a cable
    d.add(arrow(78, ROW + 22, 170, ROW + 22, DARK, 2.4))
    d.add(caption(124, ROW + 29, "CABLE", 8.5, DARK, True))
    d.add(caption(124, ROW + 13, "a wire — always works", 7))

    # router -> rover: the one radio hop
    d.add(arrow(224, ROW + 22, 326, ROW + 22, BLUE, 2.4, dash=True))
    d.add(caption(275, ROW + 29, "Wi-Fi", 8.5, BLUE, True))
    d.add(caption(275, ROW + 13, "the only wireless part", 7))

    # joystick feeds the laptop
    d.add(gamepad(28, 96, 1.0))
    d.add(caption(45, 84, "joystick", 7.5))
    d.add(arrow(45, 116, 45, ROW - 3, GREEN, 1.8))
    d.add(caption(92, 106, "plugs into the laptop,", 7, anchor="start"))
    d.add(caption(92, 96, "not into the rover", 7, anchor="start"))

    # inside the rover
    d.add(chip(352, 96, 1.0, "ESP32 board"))
    d.add(arrow(365, ROW - 6, 365, 118, GREY, 1.8))
    d.add(caption(397, 142, "USB cable", 7, GREY, anchor="start"))
    d.add(caption(397, 132, "inside the rover", 7, GREY, anchor="start"))
    d.add(caption(365, 70, "turns messages into", 7))
    d.add(caption(365, 60, "motor movement", 7))

    d.add(Line(20, 44, 440, 44, strokeColor=LIGHT, strokeWidth=0.8))
    d.add(caption(230, 26, "One wire, one radio hop, one USB cable. That is the whole path.",
                  9.5, DARK, True))
    return d


def page_channels():
    """Four things share the single Wi-Fi link, drawn to scale by size."""
    d = Drawing(460, 250)
    d.add(Rect(0, 0, 460, 250, fillColor=PAPER, strokeColor=LIGHT))

    rows = [
        (gamepad, "You steer", "laptop -> rover", "tiny", 3, GREEN),
        (gauge, "Rover reports back", "rover -> laptop", "small", 8, BLUE),
        (camera, "Camera pictures", "rover -> laptop", "BIG", 86, AMBER),
        (terminal, "You type commands", "both ways", "tiny", 3, GREY),
    ]
    y = 198
    for icon, what, direction, size_word, pct, colour in rows:
        d.add(icon(18, y - 4, 0.85))
        d.add(String(70, y + 9, what, fontName="Helvetica-Bold", fontSize=10, fillColor=DARK))
        d.add(String(70, y - 2, direction, fontName="Helvetica", fontSize=8, fillColor=GREY))
        # proportion bar
        d.add(Rect(205, y - 1, 200, 12, fillColor=WHITE, strokeColor=LIGHT))
        d.add(Rect(205, y - 1, max(4, 200 * pct / 100.0), 12, fillColor=colour, strokeColor=None))
        d.add(String(412, y + 2, size_word, fontName="Helvetica-Bold", fontSize=8, fillColor=colour))
        y -= 46

    d.add(String(230, 22, "The camera uses almost all of the space.",
                 fontName="Helvetica-Bold", fontSize=9.5, fillColor=DARK, textAnchor="middle"))
    d.add(String(230, 10, "Steering needs almost none — which is why it keeps working when video struggles.",
                 fontName="Helvetica", fontSize=8.5, fillColor=GREY, textAnchor="middle"))
    return d


def page_screen():
    """A simplified picture of the operator window."""
    d = Drawing(460, 250)
    d.add(Rect(0, 0, 460, 250, fillColor=PAPER, strokeColor=LIGHT))
    d.add(Rect(14, 16, 432, 220, rx=4, ry=4, fillColor=colors.HexColor("#101620"),
               strokeColor=DARK))

    # header strip
    d.add(Rect(14, 206, 432, 30, fillColor=colors.HexColor("#1b2430"), strokeColor=None))
    d.add(String(26, 219, "ROVER MISSION CONTROL", fontName="Helvetica-Bold",
                 fontSize=9, fillColor=WHITE))
    for i, (lbl, col) in enumerate([("ROVER LINK", GREEN), ("JETSON", GREEN),
                                    ("25:00", BLUE), ("E-STOP", RED)]):
        x = 210 + i * 58
        d.add(Rect(x, 213, 52, 15, rx=2, ry=2, fillColor=col, strokeColor=None))
        d.add(String(x + 26, 218, lbl, fontName="Helvetica-Bold", fontSize=6.5,
                     fillColor=WHITE, textAnchor="middle"))

    # video pane
    d.add(Rect(24, 74, 252, 124, fillColor=colors.HexColor("#05070a"), strokeColor=GREY))
    d.add(camera(130, 128, 1.1))
    d.add(String(150, 110, "LIVE CAMERA", fontName="Helvetica-Bold", fontSize=8.5,
                 fillColor=GREY, textAnchor="middle"))
    # picture-in-picture
    d.add(Rect(214, 80, 56, 34, fillColor=colors.HexColor("#05070a"), strokeColor=BLUE))
    d.add(String(242, 94, "2nd cam", fontName="Helvetica", fontSize=6.5,
                 fillColor=GREY, textAnchor="middle"))

    # right column
    d.add(Rect(286, 150, 148, 48, rx=3, ry=3, fillColor=colors.HexColor("#1b2430"), strokeColor=GREY))
    d.add(String(294, 186, "BUTTONS", fontName="Helvetica-Bold", fontSize=7, fillColor=GREY))
    for i, lbl in enumerate(["EXCAVATE", "DUMP"]):
        d.add(Rect(294 + i * 72, 165, 66, 14, rx=2, ry=2, fillColor=colors.HexColor("#243246"), strokeColor=None))
        d.add(String(327 + i * 72, 169, lbl, fontName="Helvetica-Bold", fontSize=6.5,
                     fillColor=WHITE, textAnchor="middle"))
    d.add(Rect(294, 153, 138, 9, rx=2, ry=2, fillColor=RED, strokeColor=None))
    d.add(String(363, 155, "EMERGENCY STOP", fontName="Helvetica-Bold", fontSize=6,
                 fillColor=WHITE, textAnchor="middle"))

    d.add(Rect(286, 112, 148, 32, rx=3, ry=3, fillColor=colors.HexColor("#1b2430"), strokeColor=GREY))
    d.add(String(294, 133, "STATUS", fontName="Helvetica-Bold", fontSize=7, fillColor=GREY))
    d.add(String(294, 121, "what the rover says it is doing", fontName="Helvetica",
                 fontSize=6.5, fillColor=GREY))

    d.add(Rect(286, 74, 148, 32, rx=3, ry=3, fillColor=colors.HexColor("#0d1117"), strokeColor=GREY))
    d.add(String(294, 95, "TERMINAL", fontName="Helvetica-Bold", fontSize=7, fillColor=GREY))
    d.add(String(294, 83, "$ type commands to the rover", fontName="Courier",
                 fontSize=6.5, fillColor=GREEN))

    # telemetry strip
    for i, lbl in enumerate(["POWER", "MOTION", "ANGLE", "SENSORS", "LINK"]):
        x = 24 + i * 84
        d.add(Rect(x, 26, 78, 40, rx=3, ry=3, fillColor=colors.HexColor("#1b2430"), strokeColor=GREY))
        d.add(String(x + 6, 54, lbl, fontName="Helvetica-Bold", fontSize=6.5, fillColor=GREY))
        d.add(String(x + 6, 40, "live numbers", fontName="Helvetica", fontSize=6, fillColor=GREY))
    return d


def _v(package, fallback):
    """Installed version of a dependency, trimmed to major.minor."""
    try:
        import importlib.metadata as md
        return ".".join(md.version(package).split(".")[:2])
    except Exception:
        return fallback


def _cv():
    try:
        import cv2
        return ".".join(cv2.__version__.split(".")[:2])
    except Exception:
        return "4.5"


def _stackbox(x, y, w, h, title, sub, fill=None, stroke=None):
    """One labelled layer in the stack diagram."""
    g = Group()
    g.add(Rect(x, y, w, h, rx=3, ry=3,
               fillColor=fill or WHITE, strokeColor=stroke or LIGHT, strokeWidth=1))
    g.add(String(x + w / 2, y + h - 13, title, fontName="Helvetica-Bold",
                 fontSize=8.5, fillColor=DARK, textAnchor="middle"))
    if sub:
        g.add(String(x + w / 2, y + h - 24, sub, fontName="Helvetica",
                     fontSize=7, fillColor=GREY, textAnchor="middle"))
    return g


def page_techstack():
    """Two stacks facing each other, with the three links drawn between them."""
    d = Drawing(460, 262)
    LW, RX, W = 8, 268, 184

    # The two machines.
    for x, name, addr in ((LW, "MCC LAPTOP", "192.168.2.50"), (RX, "JETSON ORIN NANO", "192.168.2.20")):
        d.add(Rect(x - 6, 4, W + 12, 252, rx=5, ry=5,
                   fillColor=PAPER, strokeColor=BLUE, strokeWidth=1.2))
        d.add(String(x + W / 2, 243, name, fontName="Helvetica-Bold",
                     fontSize=10, fillColor=BLUE, textAnchor="middle"))
        d.add(String(x + W / 2, 233, addr, fontName="Helvetica",
                     fontSize=7.5, fillColor=GREY, textAnchor="middle"))

    # Laptop stack, top to bottom.
    d.add(_stackbox(LW, 190, W, 36, "Mission Control window", "PySide6 / Qt 6  -  one UI thread"))
    d.add(_stackbox(LW, 152, W, 34, "State manager + event bus", "Qt signals, queued across threads"))
    d.add(_stackbox(LW, 106, W, 40, "ROS 2 worker thread", "rclpy  -  its own context", WHITE, BLUE))
    d.add(_stackbox(LW, 60, W, 40, "SSH session", "paramiko PTY + pyte VT100", WHITE, AMBER))
    d.add(_stackbox(LW, 14, W, 40, "Video player", "OpenCV + GStreamer", WHITE, GREEN))

    # Jetson stack, aligned so each link is horizontal.
    d.add(_stackbox(RX, 190, W, 36, "Mission / autonomy nodes", "nav2, localization, excavation"))
    d.add(_stackbox(RX, 152, W, 34, "Sensor drivers", "Livox MID-360, IMU, encoders"))
    d.add(_stackbox(RX, 106, W, 40, "ROS 2 nodes", "publish telemetry, subscribe cmd", WHITE, BLUE))
    d.add(_stackbox(RX, 60, W, 40, "sshd", "shell for commands", WHITE, AMBER))
    d.add(_stackbox(RX, 14, W, 40, "Camera pipelines", "GStreamer -> network", WHITE, GREEN))

    # The three links. Telemetry is the only two-way one.
    d.add(arrow(LW + W + 8, 132, RX - 8, 132, BLUE, 2.0))
    d.add(arrow(RX - 8, 120, LW + W + 8, 120, BLUE, 2.0))
    d.add(caption(230, 139, "ROS 2 topics", 7.5, BLUE, True))
    d.add(caption(230, 109, "DDS, domain 17", 6.5, GREY))

    d.add(arrow(LW + W + 8, 80, RX - 8, 80, AMBER, 2.0))
    d.add(caption(230, 85, "SSH :22", 7.5, AMBER, True))

    d.add(arrow(RX - 8, 34, LW + W + 8, 34, GREEN, 2.0))
    d.add(caption(230, 39, "video stream", 7.5, GREEN, True))

    d.add(String(230, 6, "three separate paths", fontSize=6.5,
                 fillColor=GREY, textAnchor="middle"))
    return d


def build(path="docs/How_It_Works.pdf"):
    app, rover_cfg = load_config()
    minutes = app.mission.run_duration_seconds // 60
    story = []

    # ---------------------------------------------------------- page 1
    story += [
        P("How the laptop talks to the rover", "title"),
        P(f"A picture-first explanation &nbsp;·&nbsp; {date.today():%B %Y}", "sub"),
        page_overview(),
        Spacer(1, 10),
        P("The short version", "h"),
        P("You sit at a <b>laptop</b>. The laptop is joined by a <b>cable</b> to a "
          "<b>Wi-Fi router</b> standing in the arena. The router talks by radio to the "
          "<b>rover</b>. Inside the rover a small <b>ESP32 board</b> turns the messages "
          "into motor movement."),
        P("There is only <b>one wireless step</b> in the whole chain — router to rover. "
          "Everything else is a wire. Fewer radios means fewer things that can go wrong."),
        P("Why the laptop is wired and not on Wi-Fi: the competition rules forbid the "
          "laptop from joining the rover's wireless network. Breaking that rule "
          "disqualifies the team, so the laptop's Wi-Fi is switched off.", "note"),
    ]

    story += [PageBreak()]

    # ---------------------------------------------------------- page 2
    story += [
        P("What travels over the link", "h"),
        P("Four different things share that single radio hop. They are very different sizes:"),
        page_channels(),
        Spacer(1, 8),
        P("This is the useful part to remember", "big"),
        P("Video is about <b>nine tenths</b> of everything being sent. Steering is a "
          "rounding error next to it."),
        P("So when the signal gets weak, the picture goes blocky <b>first</b>, and "
          "steering keeps working <b>longest</b>. If the video looks bad, that is a "
          "warning, not an emergency — you can still drive."),
    ]

    story += [PageBreak()]

    # ---------------------------------------------------------- page 3
    story += [
        P("Starting everything, in order", "h"),
        P("Five steps. Do them in this order — each one needs the one before it."),
    ]
    steps = [
        ("1", "Switch the rover on",
         "It takes about a minute to wake up."),
        ("2", "Check they can see each other",
         "On the laptop, ask for the list of rover topics. If the list is empty, stop "
         "and fix that first — nothing else will work."),
        ("3", "Start the joystick",
         "The joystick plugs into the <b>laptop</b>, not the rover."),
        ("4", "Open Mission Control",
         "The two badges at the top should turn green: <b>ROVER LINK</b> and <b>JETSON</b>."),
        ("5", "Press START STREAM",
         "The camera pictures appear. Now you are ready to drive."),
    ]
    for num, title, text in steps:
        d = Drawing(460, 34)
        d.add(Circle(14, 17, 12, fillColor=BLUE, strokeColor=None))
        d.add(String(14, 13, num, fontName="Helvetica-Bold", fontSize=13,
                     fillColor=WHITE, textAnchor="middle"))
        d.add(String(36, 20, title, fontName="Helvetica-Bold", fontSize=11, fillColor=DARK))
        d.add(Line(36, 14, 440, 14, strokeColor=LIGHT, strokeWidth=0.8))
        story += [d, Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{text}", ST["p"])]

    story += [
        Spacer(1, 6),
        HRFlowable(width="100%", thickness=0.7, color=LIGHT, spaceAfter=8),
        P("If the rover will not move", "big"),
        P("Work through it in this order, because the earlier causes are far more "
          "common than the later ones:"),
        P("<b>1.</b> Are both machines using the same radio settings? &nbsp; "
          "<b>2.</b> Is the joystick actually sending? &nbsp; "
          "<b>3.</b> Is the rover's controller board plugged in?"),
        P("The rover stops itself if it stops hearing from the laptop for half a second. "
          "That is deliberate — a rover that keeps driving when it has gone deaf is "
          "dangerous.", "note"),
    ]

    story += [PageBreak()]

    # ---------------------------------------------------------- page 4
    story += [
        P("What you see on screen", "h"),
        P("One window. Camera on the left, controls on the right, numbers along the bottom."),
        page_screen(),
        Spacer(1, 10),
        P("Reading it at a glance", "big"),
        P("<b>Top bar</b> — two green badges mean the rover is talking to you. The big "
          f"number is the run clock, counting down from <b>{minutes} minutes</b>."),
        P("<b>Left</b> — what the rover can see. The small inset is the second camera."),
        P("<b>Right</b> — the buttons, what the rover says it is doing, and a terminal "
          "for typing commands straight to it."),
        P("<b>Bottom</b> — battery, speed, tilt, sensor health, and link quality."),
        Spacer(1, 6),
        P("One rule worth knowing: the screen shows what the <b>rover reports</b>, never "
          "what you pressed. If you press a button and the screen does not change, the "
          "rover did not do it. That is the display being honest with you, not a bug.",
          "note"),
    ]

    story += [PageBreak()]

    # ---------------------------------------------------------- page 5
    rows = [
        ["Where", "What it is", "Doing what"],
        ["Laptop", "Ubuntu 22.04 + ROS 2 Humble", "The base both machines share"],
        ["Laptop", f"PySide6 {_v('PySide6', '6.11')} (Qt 6)", "Every window, button and card"],
        ["Laptop", "rclpy", "Speaks ROS 2 on a background thread"],
        ["Laptop", f"paramiko {_v('paramiko', '2.9')} + pyte {_v('pyte', '0.8')}", "The in-app Jetson terminal"],
        ["Laptop", f"OpenCV {_cv()} + GStreamer", "Decodes and draws the camera feeds"],
        ["Jetson", "JetPack + ROS 2 Humble", "Runs the rover's own nodes"],
        ["Jetson", "GStreamer", "Encodes the cameras and sends them out"],
        ["Jetson", "ESP32 firmware", "Turns messages into motor movement"],
    ]
    table = Table(rows, colWidths=[52, 168, 215], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR", (0, 1), (-1, -1), DARK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, PAPER]),
        ("GRID", (0, 0), (-1, -1), 0.4, LIGHT),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))

    story += [
        P("What the software is made of", "h"),
        P("The same two machines as before, but now showing what runs inside each one "
          "and which piece talks to which."),
        page_techstack(),
        Spacer(1, 6),
        P("Three paths, kept apart on purpose", "big"),
        P("<b>Telemetry</b> (blue) is the only two-way path: the rover publishes its "
          "numbers, the laptop publishes steering and stop commands. Both ends must agree "
          "on the same <b>domain number</b> and the same DDS software, or they see nothing "
          "of each other at all."),
        P("<b>Commands</b> (amber) is an ordinary SSH login, the same one you would get "
          "from a terminal window — it just happens to be drawn inside the app."),
        P("<b>Video</b> (green) does not use ROS at all. The pictures go straight from "
          "the Jetson's cameras to the player."),
        P("Kept apart deliberately: if the video dies, steering still works; if the terminal "
          "hangs, telemetry keeps arriving. Each runs on its own thread, so none of them "
          "can freeze the window.", "note"),
        P("The pieces, by name", "big"),
        table,
    ]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(GREY)
        canvas.drawString(18 * mm, 12 * mm, "How the laptop talks to the rover")
        canvas.drawRightString(LETTER[0] - 18 * mm, 12 * mm, f"{doc.page}")
        canvas.restoreState()

    SimpleDocTemplate(
        path, pagesize=LETTER,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=16 * mm, bottomMargin=18 * mm,
        title="How the laptop talks to the rover",
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    return path


if __name__ == "__main__":
    out = build(sys.argv[1] if len(sys.argv) > 1 else "docs/How_It_Works.pdf")
    print(f"wrote {out} ({os.path.getsize(out) / 1024:.0f} KB)")
