"""Render a resume JSON to a one-page A4 PDF.

The layout is fixed: serif type, blue name and section titles, optional
ID photo at the top right. Content decisions stay with the caller.
"""

import argparse
import json
from io import BytesIO
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Flowable, Frame, FrameBreak, PageTemplate

SKILL_ROOT = Path(__file__).resolve().parents[1]
FONT_REGULAR = SKILL_ROOT / "fonts" / "NotoSerifSC-Regular.ttf"
FONT_BOLD = SKILL_ROOT / "fonts" / "NotoSerifSC-Bold.ttf"

BLUE = HexColor("#227AAF")
INK = HexColor("#1C1C1C")
DEGREE_SUFFIXES = ("博士后", "博士", "硕士", "本科", "专科")


def validate(data):
    if not isinstance(data, dict):
        raise ValueError("Resume must be a JSON object")

    def string(value, label):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a nonempty string")

    string(data.get("name"), "name")
    if "headline" in data:
        string(data["headline"], "headline")
    if "photo" in data:
        string(data["photo"], "photo")
    if not isinstance(data.get("contact", []), list):
        raise ValueError("contact must be an array")
    for item in data.get("contact", []):
        string(item, "contact item")
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("sections must be a nonempty array")
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("section must be an object")
        string(section.get("title"), "section title")
        entries = section.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ValueError("entries must be a nonempty array")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("entry must be an object")
            for key in ("heading", "meta", "text"):
                if key in entry:
                    string(entry[key], key)
            bullets = entry.get("bullets", [])
            if not isinstance(bullets, list):
                raise ValueError("bullets must be an array")
            for bullet in bullets:
                string(bullet, "bullet")
            if not any(entry.get(k) for k in ("heading", "meta", "text", "bullets")):
                raise ValueError("entry must contain text")


def all_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from all_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from all_strings(item)


def register_fonts(font_path, index, data):
    if font_path:
        regular = bold = TTFont("Resume", font_path, subfontIndex=index)
        pdfmetrics.registerFont(regular)
        pdfmetrics.registerFont(TTFont("Resume-Bold", font_path, subfontIndex=index))
        faces = [regular.face]
    else:
        if not FONT_REGULAR.is_file() or not FONT_BOLD.is_file():
            raise ValueError("Bundled Noto Serif SC fonts are missing; reinstall the skill")
        regular = TTFont("Resume", str(FONT_REGULAR))
        bold = TTFont("Resume-Bold", str(FONT_BOLD))
        pdfmetrics.registerFont(regular)
        pdfmetrics.registerFont(bold)
        faces = [regular.face, bold.face]
    extra = "：—–·•电话邮箱"
    chars = set("".join(all_strings(data)) + extra)
    missing = sorted(c for c in chars if not c.isspace() and all(ord(c) not in face.charToGlyph for face in faces))
    if missing:
        raise ValueError(f"Font lacks characters: {''.join(missing)}; supply another --font")


def resolve_photo(path, json_path):
    candidate = Path(path)
    if candidate.is_file():
        return candidate
    beside_json = json_path.parent / path
    if beside_json.is_file():
        return beside_json
    in_skill = SKILL_ROOT / path
    if in_skill.is_file():
        return in_skill
    raise ValueError(f"Photo not found: {path}")


def display_date(meta):
    date = meta.split(" | ", 1)[0]
    return date.replace(" - ", " – ")


def degree_of(program):
    for suffix in DEGREE_SUFFIXES:
        if program.endswith(suffix):
            return suffix, program[: -len(suffix)].strip()
    return None, program


def contact_lines(items):
    lines = []
    if not items:
        return lines
    phone = items[0]
    lines.append(phone if phone.startswith("电话") else f"电话：{phone}")
    if len(items) > 1:
        email = items[1]
        lines.append(email if email.startswith("邮箱") else f"邮箱：{email}")
    rest = [item for item in items[2:] if item]
    if rest:
        lines.append(" · ".join(rest))
    return lines


class LineText(Flowable):
    def __init__(self, text, font, size, color, before=0):
        super().__init__()
        self.text = text
        self.font = font
        self.size = size
        self.color = color
        self.before = before

    def wrap(self, aw, ah):
        self.aw = aw
        self.ascent = pdfmetrics.getAscent(self.font, self.size)
        self.descent = abs(pdfmetrics.getDescent(self.font, self.size))
        width = pdfmetrics.stringWidth(self.text, self.font, self.size)
        if width > aw + 0.6:
            raise ValueError(f"Line exceeds one line ({width:.1f} > {aw:.1f}): {self.text}")
        self.baseline = self.descent + 0.2
        return aw, self.before + self.ascent + self.descent + 0.2

    def draw(self):
        self.canv.setFillColor(self.color)
        self.canv.setFont(self.font, self.size)
        self.canv.drawString(0, self.baseline, self.text)


class Columns(Flowable):
    def __init__(self, parts, widths, font, size, color, before=0):
        super().__init__()
        self.parts = parts
        self.widths = widths
        self.font = font
        self.size = size
        self.color = color
        self.before = before

    def wrap(self, aw, ah):
        self.ascent = pdfmetrics.getAscent(self.font, self.size)
        self.descent = abs(pdfmetrics.getDescent(self.font, self.size))
        used = sum(self.widths[:-1]) + pdfmetrics.stringWidth(self.parts[-1], self.font, self.size)
        if used > aw + 0.6:
            raise ValueError(f"Line exceeds one line ({used:.1f} > {aw:.1f}): {' '.join(self.parts)}")
        self.baseline = self.descent + 0.2
        return aw, self.before + self.ascent + self.descent + 0.2

    def draw(self):
        canvas = self.canv
        canvas.setFillColor(self.color)
        canvas.setFont(self.font, self.size)
        x = 0
        for text, width in zip(self.parts, self.widths):
            canvas.drawString(x, self.baseline, text)
            x += width


class Bullet(Flowable):
    def __init__(self, text, before=4, font="Resume", size=10.5, radius=1.0):
        super().__init__()
        self.text = text
        self.before = before
        self.indent = 12
        self.font = font
        self.size = size
        self.radius = radius

    def wrap(self, aw, ah):
        self.ascent = pdfmetrics.getAscent(self.font, self.size)
        self.descent = abs(pdfmetrics.getDescent(self.font, self.size))
        width = pdfmetrics.stringWidth(self.text, self.font, self.size)
        if width > aw - self.indent:
            raise ValueError(f"Bullet exceeds one line; shorten its content: {self.text}")
        self.baseline = self.descent + 0.2
        return aw, self.before + self.ascent + self.descent + 0.2

    def draw(self):
        canvas = self.canv
        canvas.setFillColor(INK)
        canvas.circle(2.4, self.baseline + self.ascent * 0.46, self.radius, stroke=0, fill=1)
        canvas.setFont(self.font, self.size)
        canvas.drawString(self.indent, self.baseline, self.text)


def education_rows(entries):
    rows = []
    for entry in entries:
        heading = entry.get("heading", "")
        if " · " not in heading or not entry.get("meta"):
            return None
        school, program = heading.split(" · ", 1)
        level, major = degree_of(program)
        if not level:
            return None
        school_line = f"{school}：{major}" if major else school
        rows.append((display_date(entry["meta"]), level, school_line))
    return rows


def entry_flowables(entry, body_width, first, emphasize=False):
    heading = entry.get("heading", "")
    meta = entry.get("meta", "")
    before_head = 8 if first else 11
    parts = []
    if " · " in heading and meta:
        org, role = heading.split(" · ", 1)
        line = f"{org}：{role} — {display_date(meta)}"
    elif heading:
        line = heading if not meta else f"{heading} — {display_date(meta)}"
    elif meta:
        line = display_date(meta)
    else:
        line = ""
    if line:
        font = "Resume-Bold" if emphasize else "Resume"
        parts.append(LineText(line, font, 10.5, INK, before=before_head))
    for index, bullet in enumerate(entry.get("bullets", [])):
        parts.append(Bullet(bullet, before=4 if index == 0 else 2))
    if entry.get("text"):
        parts.append(Bullet(entry["text"], before=4))
    return parts


def build_story(data, side_width, body_width, has_photo):
    header = [LineText(data["name"], "Resume-Bold", 22, BLUE)]
    if data.get("headline"):
        header.append(LineText(data["headline"], "Resume", 12, BLUE, before=8))
    for index, line in enumerate(contact_lines(data.get("contact", []))):
        header.append(LineText(line, "Resume", 11, BLUE, before=12 if index == 0 else 8))
    body = []
    for s_index, section in enumerate(data["sections"]):
        body.append(LineText(
            section["title"],
            "Resume-Bold",
            13,
            BLUE,
            before=2 if has_photo and s_index == 0 else (16 if s_index == 0 else 14),
        ))
        entries = section["entries"]
        rows = education_rows(entries)
        if rows:
            gap = 18
            col_w = []
            for index in range(2):
                col_w.append(max(pdfmetrics.stringWidth(row[index], "Resume", 10.5) for row in rows) + gap)
            col_w.append(body_width)
            for index, row in enumerate(rows):
                body.append(Columns(list(row), col_w, "Resume", 10.5, INK, before=8 if index == 0 else 6))
            continue
        emphasize = "工作经历" in section["title"]
        for index, entry in enumerate(entries):
            body.extend(entry_flowables(entry, body_width, index == 0, emphasize))
    return header, body


def render(data, output, photo_path):
    page_w, page_h = A4
    margin_x = 14 * mm
    margin_top = 12 * mm
    margin_bottom = 14 * mm
    full_w = page_w - 2 * margin_x
    photo = None
    photo_w = photo_h = 0
    if photo_path:
        photo = ImageReader(str(photo_path))
        photo_w = 36 * mm
        photo_h = 48 * mm

    if photo:
        gap = 6 * mm
        under = 3 * mm
        photo_x = page_w - margin_x - photo_w
        side_w = photo_x - gap - margin_x
        header, body = build_story(data, side_w, full_w, True)
        header_h = sum(flowable.wrap(side_w, page_h)[1] for flowable in header) + 4
        side_h = max(photo_h, header_h)
        side_bottom = page_h - margin_top - side_h
        body_h = side_bottom - under - margin_bottom
        if body_h <= 0:
            raise ValueError("Resume exceeds one A4 page; remove or shorten content, not font size")
        frames = [
            Frame(margin_x, side_bottom, side_w, side_h, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0),
            Frame(margin_x, margin_bottom, full_w, body_h, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0),
        ]
        story = header + [FrameBreak()] + body
    else:
        header, body = build_story(data, full_w, full_w, False)
        frames = [Frame(
            margin_x, margin_bottom, full_w, page_h - margin_top - margin_bottom,
            leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        )]
        story = header + body

    def draw_page(canvas, doc):
        if doc.page > 1:
            raise ValueError("Resume exceeds one A4 page; remove or shorten content, not font size")
        if photo is None:
            return
        canvas.saveState()
        canvas.drawImage(
            photo,
            page_w - margin_x - photo_w,
            page_h - margin_top - photo_h,
            width=photo_w,
            height=photo_h,
            preserveAspectRatio=True,
            anchor="c",
            mask="auto",
        )
        canvas.restoreState()

    buffer = BytesIO()
    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        title=data["name"],
        author=data["name"],
        leftMargin=margin_x,
        rightMargin=margin_x,
        topMargin=margin_top,
        bottomMargin=margin_bottom,
    )
    doc.addPageTemplates([PageTemplate(id="main", frames=frames, onPage=draw_page)])
    doc.build(story)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(buffer.getvalue())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--font")
    parser.add_argument("--font-index", type=int, default=0)
    args = parser.parse_args()
    try:
        if args.input.resolve() == args.output.resolve():
            raise ValueError("Input and output paths must differ")
        data = json.loads(args.input.read_text(encoding="utf-8"))
        validate(data)
        register_fonts(args.font, args.font_index, data)
        photo = resolve_photo(data["photo"], args.input.resolve()) if data.get("photo") else None
        render(data, args.output, photo)
    except Exception as error:
        parser.exit(1, f"Resume generation failed: {error}\n")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
