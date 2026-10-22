#!/usr/bin/env python3
"""Generates the Agalvaan Field Manual PDF: run it, read it, fix it.

Written for someone standing at the arena with a deadline, not for someone
reading at a desk. Every page answers one question, pictures first. The
debugging section is ordered by what has actually gone wrong on this rover,
not by what sounds most likely.

    python3 tools/generate_field_manual.py
"""

from __future__ import annotations

import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from reportlab.graphics.shapes import (  # noqa: E402
    Circle, Drawing, Group, Line, Polygon, Rect, String,
)
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import LETTER  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

# Clipart and styles are shared with the How It Works booklet so the two
# documents look like one family rather than two unrelated handouts.
from generate_how_it_works import (  # noqa: E402
    AMBER, BLUE, DARK, GREEN, GREY, LIGHT, PAPER, RED, ST, WHITE,
    P, arrow, caption, gamepad, laptop, rover, router, terminal,
)


def box(x, y, w, h, title, sub="", fill=None, stroke=None, title_colour=None):
    g = Group()
    g.add(Rect(x, y, w, h, rx=4, ry=4, fillColor=fill or WHITE,
               strokeColor=stroke or LIGHT, strokeWidth=1.1))
    g.add(String(x + w / 2, y + h - 14, title, fontName="Helvetica-Bold",
                 fontSize=9, fillColor=title_colour or DARK, textAnchor="middle"))
    if sub:
        for i, line in enumerate(sub.split("\n")):
            g.add(String(x + w / 2, y + h - 26 - i * 9, line, fontName="Helvetica",
                         fontSize=7.5, fillColor=GREY, textAnchor="middle"))
    return g


def tick(x, y, ok=True, s=1.0):
    """A green tick or a red cross."""
    g = Group()
    col = GREEN if ok else RED
    g.add(Circle(x, y, 7 * s, fillColor=col, strokeColor=None))
    if ok:
        g.add(Polygon([x - 3.4 * s, y, x - 1.2 * s, y - 2.8 * s, x + 3.6 * s, y + 3.2 * s,
                       x + 3.6 * s, y + 4.4 * s, x - 1.2 * s, y - 1.0 * s, x - 3.4 * s, y + 1.2 * s],
                      fillColor=WHITE, strokeColor=WHITE))
    else:
        for a, b in ((-1, -1), (-1, 1)):
            g.add(Line(x - 3.2 * s * a, y - 3.2 * s * b, x + 3.2 * s * a, y + 3.2 * s * b,
                       strokeColor=WHITE, strokeWidth=2.0 * s))
    return g


# ------------------------------------------------------------------ pages

def page_map():
    """Who talks to whom, and over what."""
    d = Drawing(460, 250)
    # Two-line labels are drawn as two captions: the clipart helpers take a
    # single string and render a stray newline as a black box glyph.
    d.add(laptop(55, 160, 0.95, ""))
    d.add(caption(85, 132, "YOUR LAPTOP", 8, DARK, True))
    d.add(caption(85, 122, "192.168.2.50", 7.5, GREY))

    d.add(router(225, 175, 0.95, ""))
    d.add(caption(250, 142, "ARENA Wi-Fi", 8, DARK, True))
    d.add(caption(250, 132, "Team_###", 7.5, GREY))

    d.add(rover(385, 160, 0.95, ""))
    d.add(caption(410, 132, "ROVER / JETSON", 8, DARK, True))
    d.add(caption(410, 122, "192.168.2.20", 7.5, GREY))

    d.add(gamepad(70, 88, 0.9))
    d.add(caption(84, 70, "joystick plugs in HERE", 7.5, GREY))

    d.add(arrow(120, 192, 205, 192, BLUE, 2.0))
    d.add(arrow(275, 192, 358, 192, BLUE, 2.0))
    d.add(caption(162, 199, "Wi-Fi or cable", 7, GREY))
    d.add(caption(316, 199, "Wi-Fi", 7, GREY))

    d.add(box(40, 10, 125, 46, "TELEMETRY", "battery, IMU, encoders\nrover -> laptop", PAPER, BLUE))
    d.add(box(172, 10, 125, 46, "VIDEO", "two RTSP streams\nrover -> laptop", PAPER, GREEN))
    d.add(box(304, 10, 125, 46, "COMMANDS", "typed over SSH\nlaptop -> rover", PAPER, AMBER))
    return d


def page_tree():
    """The debugging decision tree."""
    d = Drawing(460, 430)
    W, H = 300, 34
    X = 80

    def step(y, q, yes, no, colour=BLUE):
        g = Group()
        g.add(Rect(X, y, W, H, rx=4, ry=4, fillColor=PAPER, strokeColor=colour, strokeWidth=1.2))
        g.add(String(X + 10, y + H - 13, q, fontName="Helvetica-Bold", fontSize=8.5, fillColor=DARK))
        g.add(String(X + 10, y + 8, yes, fontName="Helvetica", fontSize=7.5, fillColor=GREEN))
        if no:
            g.add(String(X + W - 10, y + 8, no, fontName="Helvetica", fontSize=7.5,
                         fillColor=GREY, textAnchor="end"))
        return g

    rows = [
        (356, "1.  Does the UI show CONNECT MOCK, every card at '--' ?",
         "YES -> you started it wrong. Use  ./run_ui.sh", "no -> go to 2"),
        (304, "2.  Does  ros2 topic list  show the rover's topics?",
         "NO -> run  rover-dds-check  on BOTH machines", "yes -> go to 3"),
        (252, "3.  Did you change anything since the nodes started?",
         "YES -> new terminal, then  ros2 daemon stop", "no -> go to 4"),
        (200, "4.  Service says 'active' but nothing arrives?",
         "YES -> sudo ss -unap | grep joy_node   (wrong address)", "no -> go to 5"),
        (148, "5.  Is just ONE card blank?",
         "YES -> check that card's topic with  ros2 topic hz", "no -> go to 6"),
        (96, "6.  Is the video blank?",
         "YES -> the camera DRIVER is not running (see page 4)", "no -> go to 7"),
        (44, "7.  Still stuck?",
         "Capture:  journalctl -u rover-teleop -b   and   date", ""),
    ]
    for y, q, yes, no in rows:
        d.add(step(y, q, yes, no))
        if y > 48:
            d.add(arrow(X + W / 2, y, X + W / 2, y - 18, GREY, 1.4, head=4))

    d.add(String(230, 408, "START: something is wrong", fontName="Helvetica-Bold",
                 fontSize=10, fillColor=RED, textAnchor="middle"))
    d.add(arrow(230, 402, 230, 392, RED, 1.6, head=4))
    d.add(String(230, 26, "Work DOWN the list. Each step rules out the ones below it.",
                 fontName="Helvetica-Oblique", fontSize=7.5, fillColor=GREY, textAnchor="middle"))
    d.add(String(230, 14, "This order matches what has actually gone wrong on this rover.",
                 fontName="Helvetica-Oblique", fontSize=7.5, fillColor=GREY, textAnchor="middle"))
    return d


def page_cards():
    """What each card on the screen reads from."""
    d = Drawing(460, 210)
    specs = [
        (8, "POWER", "/battery_state", "from rover-power", GREEN),
        (120, "ATTITUDE", "/witmotion/imu", "from rover-sensors", GREEN),
        (232, "MOTION", "/wheel/odom", "from rover-sensors", AMBER),
        (344, "SENSORS", "/diagnostics", "HEALTH reports,\nnot sensor data", BLUE),
    ]
    for x, name, topic, who, col in specs:
        d.add(box(x, 112, 104, 80, name, f"reads\n{topic}\n\n{who}", PAPER, col))
    d.add(box(8, 20, 216, 74, "LOC  (stream footer)",
              "green = AprilTag fixes arriving\nred = no fix   grey = no telemetry\nreads /tag/rover_pose", PAPER, GREEN))
    d.add(box(240, 20, 212, 74, "STREAMS",
              "D456  rtsp://192.168.2.20:8554/stream\nOV9281 rtsp://192.168.2.20:8555/rear\nDRIVERS are started by hand", PAPER, AMBER))
    return d


def build(path="docs/Field_Manual.pdf"):
    story = []

    # ---------------------------------------------------------- page 1
    story += [
        P("Agalvaan Field Manual", "title"),
        P(f"Run it, read it, fix it &nbsp;·&nbsp; {date.today():%B %Y}", "sub"),
        page_map(),
        Spacer(1, 8),
        P("The three things that travel", "h"),
        P("<b>Telemetry</b> is the rover reporting on itself — battery, tilt, wheel "
          "speed. <b>Video</b> is two camera streams. <b>Commands</b> are typed into "
          "the rover's terminal over SSH."),
        P("Mission Control <b>never publishes to the rover</b>. It listens, and it "
          "types commands into a shell. That is deliberate: nothing the operator "
          "does can write into the rover's own message traffic."),
        P("Both machines have <b>fixed addresses</b> (.50 and .20). The arena Wi-Fi "
          "name can change without anything else changing — see page 5.", "note"),
    ]

    # ---------------------------------------------------------- page 2
    story += [PageBreak(), P("Starting up, in order", "h"),
              P("Each step needs the one before it.")]
    steps = [
        ("1", "Power the arena router FIRST",
         "If the rover boots with no router, it cannot find the network and will sit on the wrong one."),
        ("2", "Power the rover, wait about a minute",
         "Teleop, battery logging and the rear camera all start by themselves."),
        ("3", "Open the laptop; it joins automatically",
         "Check with <b>ip -br addr</b> — you want <b>192.168.2.50</b>."),
        ("4", "Plug the joystick into the LAPTOP, then start it",
         "<b>ros2 run joy joy_node</b> — the rover reads it over the network."),
        ("5", "Start Mission Control",
         "<b>./run_ui.sh</b> — never <b>python3 -m mission_control.main</b>, which "
         "silently shows FAKE data."),
        ("6", "Start the front camera by hand",
         "<b>ros2 launch rover_bringup camera.launch.py</b> on the rover. The rear "
         "camera is already running."),
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
        P("Two minutes of checks before a run", "big"),
        P("<b>rover-dds-check</b> on both machines &nbsp;·&nbsp; <b>date</b> on the "
          "rover (it must not say 1970) &nbsp;·&nbsp; the POWER card shows volts "
          "&nbsp;·&nbsp; the joystick moves the wheels."),
    ]

    # ---------------------------------------------------------- page 3
    story += [PageBreak(), P("When something is wrong", "h"),
              P("Start at the top. Do not skip ahead — step 1 is the most common "
                "fault by a wide margin."),
              page_tree()]

    # ---------------------------------------------------------- page 4
    story += [PageBreak(), P("What each part of the screen reads", "h"),
              P("A blank card is not a broken card. It means its topic is silent, "
                "which usually means the thing that publishes it is not running."),
              page_cards(), Spacer(1, 10),
              P("The one that confuses people", "big"),
              P("<b>SENSORS reports health, not data.</b> The IMU can be publishing "
                "perfectly while SENSORS says nothing about it, because that card "
                "reads health reports and the rover does not publish one for the IMU. "
                "ATTITUDE showing numbers is the proof the IMU is fine."),
              P("If the rover explicitly reports a sensor as DEGRADED, that always "
                "wins over 'messages are arriving'. The rover knowing something is "
                "wrong beats us guessing from traffic.", "note")]

    # ---------------------------------------------------------- page 5
    rows = [
        ["Symptom", "Cause", "Fix"],
        ["Every card '--', CONNECT MOCK shown", "UI started the wrong way", "./run_ui.sh"],
        ["ros2 topic list is empty", "RMW / domain / URI mismatch", "rover-dds-check on BOTH"],
        ["Topics listed but no data", "stale ros2 daemon", "ros2 daemon stop"],
        ["Service 'active', nothing arrives", "node bound the OLD address", "restart that service"],
        ["MOTION blank, ATTITUDE fine", "encoder ESP sending nothing", "power the motor electronics"],
        ["SENSORS says nothing for IMU", "no IMU health report exists", "normal — check ATTITUDE"],
        ["Video blank", "camera driver not running", "launch camera.launch.py"],
        ["Rover clock says 1970", "no RTC battery", "rover-clock restores it"],
        ["Changed something, no effect", "process read config at START", "restart that process"],
    ]
    table = Table(rows, colWidths=[168, 140, 132], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR", (0, 1), (-1, -1), DARK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, PAPER]),
        ("GRID", (0, 0), (-1, -1), 0.4, LIGHT),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [PageBreak(), P("Symptom to fix, at a glance", "h"), table,
              Spacer(1, 12),
              P("If the arena Wi-Fi name is not Team_1", "h"),
              P("Two commands, one on each machine. Nothing else changes."),
              Paragraph(
                  '<font face="Courier" size="8.5">'
                  'sudo nmcli con modify Team_1 802-11-wireless.ssid "Team_7" \\<br/>'
                  '&nbsp;&nbsp;&nbsp;&nbsp;wifi-sec.psk "&lt;new-password&gt;"<br/>'
                  'sudo nmcli con up Team_1</font>', ST["p"]),
              P("Everything else is keyed on the two fixed addresses, and those are "
                "<b>ours</b>, not the router's. The two machines keep talking even if "
                "the router hands out a completely different range."),
              P("The one thing that would really break it is being forced onto "
                "automatic addresses. Then the addresses move and the rover and "
                "laptop lose each other. Keep both ends fixed.", "note")]

    # ---------------------------------------------------------- page 6
    story += [PageBreak(), P("Commands worth knowing", "h")]
    cmds = [
        ("Can this terminal talk to the rover?", "rover-dds-check"),
        ("Start Mission Control", "./run_ui.sh"),
        ("After changing ANY setting", "ros2 daemon stop   + open a new terminal"),
        ("What is the rover publishing?", "ros2 topic list"),
        ("Is a topic actually flowing?", "ros2 topic hz /witmotion/imu"),
        ("Which address did a node bind?", "sudo ss -unap | grep joy_node"),
        ("Are the video streams being served?", "sudo ss -tlnp | grep -E ':8554|:8555'"),
        ("Start the front camera", "ros2 launch rover_bringup camera.launch.py"),
        ("Service logs, this boot only", "journalctl -u rover-teleop -b"),
        ("Rover services at a glance", "systemctl status rover-teleop rover-power rover-video"),
    ]
    for what, cmd in cmds:
        d = Drawing(460, 30)
        d.add(Rect(0, 2, 460, 26, rx=3, ry=3, fillColor=PAPER, strokeColor=LIGHT))
        d.add(String(8, 17, what, fontName="Helvetica-Bold", fontSize=8, fillColor=DARK))
        d.add(String(8, 6, cmd, fontName="Courier", fontSize=8, fillColor=BLUE))
        story += [d]

    story += [Spacer(1, 10),
              P("One rule that explains most surprises", "big"),
              P("<b>Programs read their settings once, when they start.</b> Changing "
                "a file or an address does nothing to something already running. "
                "After any change: restart the thing, and open a fresh terminal.")]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(GREY)
        canvas.drawString(18 * mm, 12 * mm, "Agalvaan Field Manual")
        canvas.drawRightString(LETTER[0] - 18 * mm, 12 * mm, f"{doc.page}")
        canvas.restoreState()

    SimpleDocTemplate(
        path, pagesize=LETTER,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=16 * mm, bottomMargin=18 * mm,
        title="Agalvaan Field Manual",
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    return path


if __name__ == "__main__":
    out = build(sys.argv[1] if len(sys.argv) > 1 else "docs/Field_Manual.pdf")
    print(f"wrote {out} ({os.path.getsize(out) / 1024:.0f} KB)")
