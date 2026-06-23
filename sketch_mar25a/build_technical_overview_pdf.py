#!/usr/bin/env python3
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


SOURCE = Path("technical_program_overview.md")
OUTPUT = Path("technical_program_overview.pdf")


def escape(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def inline_markup(text):
    escaped = escape(text)
    parts = []
    code_open = False
    token = []
    for char in escaped:
        if char == "`":
            if code_open:
                parts.append(
                    '<font face="Courier" backColor="#eef0e8">'
                    + "".join(token)
                    + "</font>"
                )
                token = []
                code_open = False
            else:
                parts.append("".join(token))
                token = []
                code_open = True
        else:
            token.append(char)
    parts.append("".join(token))
    if code_open:
        return escaped.replace("`", "")
    return "".join(parts)


def build():
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "TitleCustom",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=22,
        leading=27,
        textColor=colors.HexColor("#20302b"),
        spaceAfter=14,
    )
    h1 = ParagraphStyle(
        "Heading1Custom",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=19,
        textColor=colors.HexColor("#0f766e"),
        spaceBefore=14,
        spaceAfter=7,
    )
    h2 = ParagraphStyle(
        "Heading2Custom",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12.5,
        leading=16,
        textColor=colors.HexColor("#20302b"),
        spaceBefore=10,
        spaceAfter=5,
    )
    body = ParagraphStyle(
        "BodyCustom",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9.6,
        leading=13.2,
        spaceAfter=5,
    )
    bullet = ParagraphStyle(
        "BulletCustom",
        parent=body,
        leftIndent=14,
        firstLineIndent=-8,
    )
    code = ParagraphStyle(
        "CodeCustom",
        parent=body,
        fontName="Courier",
        fontSize=8.5,
        leading=11,
        leftIndent=10,
        backColor=colors.HexColor("#eef0e8"),
        borderPadding=4,
        spaceBefore=3,
        spaceAfter=6,
    )

    doc = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        rightMargin=1.8 * cm,
        leftMargin=1.8 * cm,
        topMargin=1.6 * cm,
        bottomMargin=1.6 * cm,
        title="Rowing IMU Prototype - Program and Metric Overview",
    )

    story = []
    lines = SOURCE.read_text().splitlines()
    pending_table = []
    in_code = False
    code_lines = []

    def flush_table():
        nonlocal pending_table
        if not pending_table:
            return
        rows = [[Paragraph(inline_markup(cell), body) for cell in row] for row in pending_table]
        table = Table(rows, colWidths=[4.5 * cm, 11.0 * cm])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6f0ed")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#20302b")),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cad4ce")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(table)
        story.append(Spacer(1, 6))
        pending_table = []

    for line in lines:
        if line.startswith("```"):
            if in_code:
                story.append(Paragraph("<br/>".join(escape(x) for x in code_lines), code))
                code_lines = []
                in_code = False
            else:
                flush_table()
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        if not line.strip():
            flush_table()
            story.append(Spacer(1, 4))
            continue

        if line.startswith("# "):
            flush_table()
            story.append(Paragraph(inline_markup(line[2:]), title))
            story.append(Spacer(1, 4))
        elif line.startswith("## "):
            flush_table()
            story.append(Paragraph(inline_markup(line[3:]), h1))
        elif line.startswith("### "):
            flush_table()
            story.append(Paragraph(inline_markup(line[4:]), h2))
        elif line.startswith("- "):
            flush_table()
            story.append(Paragraph("&bull; " + inline_markup(line[2:]), bullet))
        elif line.startswith("`") and line.endswith("`"):
            flush_table()
            story.append(Paragraph(escape(line.strip("`")), code))
        else:
            flush_table()
            story.append(Paragraph(inline_markup(line), body))

    flush_table()
    doc.build(story)


if __name__ == "__main__":
    build()
