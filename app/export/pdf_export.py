"""Экспорт протокола в PDF (fpdf2). Шрифт Montserrat (OFL) — кириллица + казахские буквы."""
from __future__ import annotations

import os

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

from .common import prepare

FONT_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "static", "fonts", "pdf"))
FONT = "Montserrat"

INK = (28, 32, 40)
MUTED = (105, 112, 125)
ACCENT = (38, 70, 140)
RULE = (200, 205, 215)
HEAD_BG = (232, 237, 246)


def _md(t: str) -> str:
    """Экранирует маркеры markdown fpdf2 (**, __, --, ~~) в пользовательском тексте."""
    t = t or ""
    for a, b in (("**", "*​*"), ("__", "_​_"), ("--", "-​-"), ("~~", "~​~")):
        while a in t:
            t = t.replace(a, b)
    return t


class _PDF(FPDF):
    footer_text = ""

    def footer(self):
        self.set_y(-12)
        self.set_font(FONT, "", 7)
        self.set_text_color(*MUTED)
        self.cell(0, 5, self.footer_text, align="L")
        self.set_x(self.l_margin)
        self.cell(0, 5, f"Стр. {self.page_no()} из {{nb}}", align="R")


def _new_pdf(footer: str) -> _PDF:
    pdf = _PDF(orientation="P", unit="mm", format="A4")
    pdf.footer_text = footer
    pdf.add_font(FONT, "", os.path.join(FONT_DIR, "Montserrat-Regular.ttf"))
    pdf.add_font(FONT, "B", os.path.join(FONT_DIR, "Montserrat-Bold.ttf"))
    pdf.add_font(FONT, "I", os.path.join(FONT_DIR, "Montserrat-Italic.ttf"))
    # «BI» нет отдельного файла — используем Bold
    pdf.add_font(FONT, "BI", os.path.join(FONT_DIR, "Montserrat-Bold.ttf"))
    pdf.set_margins(18, 16, 18)
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_title("Протокол совещания")
    pdf.set_creator("AI Hatshy")
    pdf.add_page()
    return pdf


def _h(pdf: _PDF, text: str):
    if pdf.get_y() > pdf.h - 45:
        pdf.add_page()
    pdf.ln(4)
    pdf.set_font(FONT, "B", 12)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 7, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    y = pdf.get_y()
    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.3)
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(2)
    pdf.set_text_color(*INK)


def _p(pdf: _PDF, text: str, size=9.5, style="", color=INK, h=5, markdown=False):
    pdf.set_font(FONT, style, size)
    pdf.set_text_color(*color)
    pdf.multi_cell(0, h, text, markdown=markdown, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _bullets(pdf: _PDF, items: list[str], numbered=False):
    pdf.set_font(FONT, "", 9.5)
    pdf.set_text_color(*INK)
    for i, it in enumerate(items, 1):
        mark = f"{i}." if numbered else "•"
        pdf.set_x(pdf.l_margin + 2)
        pdf.cell(6, 5, mark)
        pdf.multi_cell(0, 5, it, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(0.8)


def build_pdf(meeting: dict, sections, anon: bool) -> bytes:
    d = prepare(meeting, sections, anon)
    sec = d["sections"]
    pdf = _new_pdf(d["footer"])
    head_style = FontFace(family=FONT, emphasis="BOLD", size_pt=8.5, color=INK, fill_color=HEAD_BG)

    # --- шапка
    pdf.set_font(FONT, "B", 18)
    pdf.set_text_color(*INK)
    pdf.cell(0, 10, d["title"], align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if d["org"]:
        pdf.set_font(FONT, "I", 10)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 6, d["org"], align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    _p(pdf, f"**Тема:** {_md(d['topic'])}", size=11, h=6, markdown=True)
    pdf.ln(1)
    if d["meta"]:
        _p(pdf, "     ".join(f"**{k}:** {_md(v)}" for k, v in d["meta"]), size=9, color=MUTED, markdown=True)
    if d["participants"]:
        _p(pdf, f"**Участники ({len(d['participants'])}):** " + _md(", ".join(d["participants"])),
           size=9, color=MUTED, markdown=True)
    if d["anon"]:
        _p(pdf, "Персональные данные обезличены: ФИО заменены на «Участник N».", size=8, style="I", color=MUTED)

    # --- краткое содержание и решения
    if "summary" in sec:
        _h(pdf, "Краткое содержание")
        _p(pdf, d["summary"] or "Краткое содержание не сформировано.", h=5.2)
        if d["topics"]:
            pdf.ln(2)
            _p(pdf, "**Обсуждавшиеся вопросы:**", markdown=True)
            _bullets(pdf, [f"{lbl} ({t})" if t else lbl for lbl, t in d["topics"]], numbered=True)
        if d["numbers"]:
            pdf.ln(1)
            _p(pdf, "**Ключевые показатели:**", markdown=True)
            _bullets(pdf, [f"{v} — {m}" if m else v for v, m in d["numbers"]])
        _h(pdf, "Принятые решения")
        if d["decisions"]:
            _bullets(pdf, d["decisions"], numbered=True)
        else:
            _p(pdf, "Отдельные решения не зафиксированы.", color=MUTED)

    # --- поручения
    if "tasks" in sec:
        _h(pdf, "Поручения")
        if d["tasks"]:
            pdf.set_font(FONT, "", 8.5)
            pdf.set_text_color(*INK)
            with pdf.table(col_widths=(8, 92, 44, 30), text_align=("CENTER", "LEFT", "LEFT", "CENTER"),
                           headings_style=head_style, line_height=4.3, padding=1.6,
                           borders_layout="HORIZONTAL_LINES", markdown=True,
                           first_row_as_headings=True, repeat_headings=1) as table:
                hr = table.row()
                for c in ("№", "Поручение", "Ответственный", "Срок"):
                    hr.cell(c)
                for t in d["tasks"]:
                    r = table.row()
                    r.cell(t["n"])
                    body = _md(t["title"])
                    if t["done"]:
                        body += "  **[выполнено]**"
                    if t["quote"]:
                        body += f"\n__«{_md(t['quote'])}»" + (f" [{t['t']}]" if t["t"] else "") + "__"
                    r.cell(body)
                    own = f"**{_md(t['owner'])}**"
                    if t["co_owners"]:
                        own += "\nсоисполн.: " + _md(", ".join(t["co_owners"]))
                    r.cell(own)
                    r.cell(_md(t["due"]))
        else:
            _p(pdf, "Поручения не выявлены.", color=MUTED)

    # --- стенограмма
    if "transcript" in sec:
        _h(pdf, "Стенограмма")
        if d["transcript"]:
            for seg in d["transcript"]:
                lang = f" ({seg['lang']})" if seg["lang"] else ""
                pdf.set_font(FONT, "", 8.8)
                pdf.set_text_color(*INK)
                pdf.multi_cell(0, 4.6, f"**[{seg['t']}] {_md(seg['name'])}**{lang}: {_md(seg['text'])}",
                               markdown=True, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.ln(1.2)
        else:
            _p(pdf, "Стенограмма отсутствует.", color=MUTED)

    # --- участники
    if "people" in sec and d["people"]:
        _h(pdf, "Участники")
        pdf.set_font(FONT, "", 8.8)
        pdf.set_text_color(*INK)
        with pdf.table(col_widths=(8, 58, 84, 24), text_align=("CENTER", "LEFT", "LEFT", "CENTER"),
                       headings_style=head_style, line_height=4.5, padding=1.6,
                       borders_layout="HORIZONTAL_LINES", first_row_as_headings=True) as table:
            hr = table.row()
            for c in ("№", "Участник", "Роль / должность", "Доля речи"):
                hr.cell(c)
            for i, p in enumerate(d["people"], 1):
                r = table.row()
                for v in (str(i), p["name"], p["role"], p["pct"]):
                    r.cell(v)

    # --- подписи
    if "sign" in sec:
        if pdf.get_y() > pdf.h - 55:
            pdf.add_page()
        pdf.ln(12)
        pdf.set_font(FONT, "", 10)
        pdf.set_text_color(*INK)
        chair = f" / {d['chair']} /" if d["chair"] else ""
        pdf.cell(0, 8, f"Председатель  ______________________{chair}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(6)
        pdf.cell(0, 8, "Секретарь  ______________________", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    return bytes(pdf.output())
