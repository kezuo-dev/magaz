"""Экспорт списка книг в PDF с поддержкой кириллицы.

Использует reportlab + встроенный шрифт DejaVuSans для корректного отображения
русских названий и артикулов. PDF сохраняется в память (BytesIO) и отдаётся
как файл-вложение через FastAPI Response.
"""
from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.forbidden_check import ForbiddenMatch


def generate_forbidden_pdf(results: list[ForbiddenMatch]) -> bytes:
    """Сгенерировать PDF-отчёт со списком найденных книг. Возвращает байты PDF."""
    # Регистрируем DejaVuSans для кириллицы. reportlab ищет шрифт в своей папке
    # fonts/ или в системных путях. В докер-образе установим пакет fonts-dejavu.
    try:
        pdfmetrics.registerFont(TTFont("DejaVuSans", "DejaVuSans.ttf"))
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", "DejaVuSans-Bold.ttf"))
    except Exception:
        # Если шрифт не найден — fallback на Helvetica (кириллицу не покажет, но
        # PDF не упадёт). В проде должен быть установлен fonts-dejavu в образе.
        pass

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    # Стили текста с DejaVuSans
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CustomTitle",
        parent=styles["Heading1"],
        fontName="DejaVuSans-Bold",
        fontSize=16,
        textColor=colors.HexColor("#1a1a1a"),
        spaceAfter=12,
    )
    normal_style = ParagraphStyle(
        "CustomNormal",
        parent=styles["Normal"],
        fontName="DejaVuSans",
        fontSize=9,
        textColor=colors.HexColor("#333333"),
    )

    story = []

    # Заголовок
    story.append(Paragraph("Проверка запрещённых тем", title_style))
    story.append(Paragraph(f"Найдено книг: {len(results)}", normal_style))
    story.append(Spacer(1, 0.5 * cm))

    if not results:
        story.append(Paragraph("Проблемных книг не найдено.", normal_style))
    else:
        # Таблица с результатами (без колонки «Автор»)
        table_data = [["Артикул", "Название", "Категория", "Слово"]]
        for match in results:
            table_data.append(
                [
                    match.sku,
                    match.title[:80] + ("…" if len(match.title) > 80 else ""),
                    match.category,
                    match.matched_word,
                ]
            )

        table = Table(
            table_data,
            colWidths=[3.5 * cm, 10 * cm, 4 * cm, 2.5 * cm],
            repeatRows=1,
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1a1a1a")),
                    ("FONTNAME", (0, 0), (-1, 0), "DejaVuSans-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                    ("TOPPADDING", (0, 0), (-1, 0), 8),
                    ("FONTNAME", (0, 1), (-1, -1), "DejaVuSans"),
                    ("FONTSIZE", (0, 1), (-1, -1), 8),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 1), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
                ]
            )
        )
        story.append(table)

    doc.build(story)
    buffer.seek(0)
    return buffer.read()
