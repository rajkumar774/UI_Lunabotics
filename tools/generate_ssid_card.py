#!/usr/bin/env python3
"""One-page card: changing the arena Wi-Fi name. Print it, carry it.

Deliberately a single page with nothing else on it. This gets done once, under
time pressure, possibly with the rover already powered — so the order of the
two machines matters more than any explanation, and it is the loudest thing
on the page.

    python3 tools/generate_ssid_card.py
"""

from __future__ import annotations

import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from reportlab.graphics.shapes import Circle, Drawing, Rect, String  # noqa: E402
from reportlab.lib.pagesizes import LETTER  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer,
)

from generate_how_it_works import (  # noqa: E402
    AMBER, BLUE, DARK, GREEN, GREY, LIGHT, PAPER, RED, ST, WHITE,
    P, arrow, caption, laptop, rover,
)

CODE = ParagraphStyle("code", fontName="Courier-Bold", fontSize=10, leading=15,
                      textColor=DARK, spaceAfter=2, leftIndent=10)


def order_strip():
    """The one thing that must not be got wrong: Jetson first."""
    d = Drawing(460, 104)
    d.add(Rect(0, 0, 460, 104, rx=5, ry=5, fillColor=PAPER,
               strokeColor=RED, strokeWidth=1.4))

    d.add(rover(70, 36, 0.78, ""))
    d.add(Circle(44, 76, 11, fillColor=RED, strokeColor=None))
    d.add(String(44, 72, "1", fontName="Helvetica-Bold", fontSize=12,
                 fillColor=WHITE, textAnchor="middle"))
    d.add(caption(95, 22, "ROVER FIRST", 9, DARK, True))

    d.add(arrow(185, 58, 265, 58, RED, 2.2))
    d.add(caption(225, 66, "then, and only then", 7.5, GREY))

    d.add(laptop(300, 40, 0.78, ""))
    d.add(Circle(286, 76, 11, fillColor=RED, strokeColor=None))
    d.add(String(286, 72, "2", fontName="Helvetica-Bold", fontSize=12,
                 fillColor=WHITE, textAnchor="middle"))
    d.add(caption(326, 22, "LAPTOP SECOND", 9, DARK, True))

    d.add(String(230, 8, "Change the laptop first and you lose SSH to the rover mid-way.",
                 fontName="Helvetica-Bold", fontSize=8, fillColor=RED, textAnchor="middle"))
    return d


def build(path="docs/SSID_Change_Card.pdf"):
    story = [
        P("If the arena Wi-Fi name changes", "title"),
        P(f"Two commands per machine &nbsp;·&nbsp; nothing else changes "
          f"&nbsp;·&nbsp; {date.today():%B %Y}", "sub"),
        order_strip(),
        Spacer(1, 10),

        P("1 &nbsp;·&nbsp; On the ROVER  (ssh agazhvan2@192.168.2.20)", "h"),
        Paragraph('sudo nmcli con modify Team_1 \\', CODE),
        Paragraph('&nbsp;&nbsp;&nbsp;&nbsp;802-11-wireless.ssid "<b>Team_7</b>" \\', CODE),
        Paragraph('&nbsp;&nbsp;&nbsp;&nbsp;wifi-sec.psk "<b>&lt;new-password&gt;</b>"', CODE),
        Paragraph('sudo nmcli con up Team_1', CODE),
        Spacer(1, 8),

        P("2 &nbsp;·&nbsp; On the LAPTOP", "h"),
        Paragraph('sudo nmcli con modify Team_1 \\', CODE),
        Paragraph('&nbsp;&nbsp;&nbsp;&nbsp;802-11-wireless.ssid "<b>Team_7</b>" \\', CODE),
        Paragraph('&nbsp;&nbsp;&nbsp;&nbsp;wifi-sec.psk "<b>&lt;new-password&gt;</b>"', CODE),
        Paragraph('sudo nmcli con up Team_1', CODE),
        Spacer(1, 6),
        P("Replace <b>Team_7</b> and the password with the real ones. Keep the "
          "profile NAME as <b>Team_1</b> — only the <b>ssid</b> field has to match "
          "the air.", "note"),

        Spacer(1, 6),
        HRFlowable(width="100%", thickness=0.7, color=LIGHT, spaceAfter=8),

        P("Then check", "h"),
        Paragraph('ip -br addr        <font color="#8794a3"># .20 on rover, .50 on laptop</font>', CODE),
        Paragraph('ping 192.168.2.20  <font color="#8794a3"># from the laptop</font>', CODE),
        Paragraph('rover-dds-check    <font color="#8794a3"># on BOTH machines</font>', CODE),

        Spacer(1, 10),
        P("Why nothing else needs changing", "big"),
        P("The Wi-Fi name exists in <b>exactly two places</b> — those two profiles. "
          "Not in the UI config, not in either DDS config, not in any startup file. "
          "Everything else is keyed on <b>192.168.2.20</b> and <b>192.168.2.50</b>, "
          "and those addresses are <b>ours</b>, not the router's — so the two "
          "machines keep talking even if the arena router hands out a completely "
          "different range."),
        P("Being forced onto automatic addresses is the one thing that would break "
          "this: the addresses move and the machines lose each other. <b>Keep both "
          "ends fixed.</b> &nbsp;·&nbsp; <b>Locked out?</b> Plug a monitor and "
          "keyboard into the Jetson and run the same two lines there — nothing is "
          "lost, the services come back on their own.", "note"),
    ]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(GREY)
        canvas.drawString(18 * mm, 12 * mm, "Agalvaan — arena Wi-Fi change card")
        canvas.drawRightString(LETTER[0] - 18 * mm, 12 * mm, "keep with the laptop")
        canvas.restoreState()

    SimpleDocTemplate(
        path, pagesize=LETTER,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=16 * mm, bottomMargin=18 * mm,
        title="Agalvaan - arena Wi-Fi change card",
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    return path


if __name__ == "__main__":
    out = build(sys.argv[1] if len(sys.argv) > 1 else "docs/SSID_Change_Card.pdf")
    print(f"wrote {out} ({os.path.getsize(out) / 1024:.0f} KB)")
