"""PDF-отчёт платной диагностики «Анализ процессов предприятия» (ТЗ 5.4.3).

ReportLab + DejaVuSans (шрифты в комплекте — slim-образ без системных шрифтов).
Структура — разделы 1–7 из /opt/alt_docs/alt_diagnostics/anketa_paid.md.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app import config
from app.diagnostics_scoring import LEVELS

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
FALLBACK_FONT_DIRS = [
    Path("/usr/share/fonts/truetype/dejavu"),
]


def _register_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for name, file in (("DejaVu", "DejaVuSans.ttf"), ("DejaVu-Bold", "DejaVuSans-Bold.ttf")):
        candidates = [FONT_DIR / file] + [d / file for d in FALLBACK_FONT_DIRS]
        for path in candidates:
            if path.exists():
                pdfmetrics.registerFont(TTFont(name, str(path)))
                break
        else:
            pdfmetrics.registerFont(TTFont(name, "Helvetica"))  # last resort: без кириллицы


def build_paid_report(
    diag_id: int,
    *,
    name: str,
    company: str,
    email: str,
    answers: dict[str, Any],
) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    _register_fonts()
    out_dir = config.DATA_DIR / "diagnostics_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"report_{diag_id}.pdf"

    h1 = ParagraphStyle("h1", fontName="DejaVu-Bold", fontSize=18, leading=24,
                        textColor=colors.HexColor("#1a5c8a"), spaceAfter=10)
    h2 = ParagraphStyle("h2", fontName="DejaVu-Bold", fontSize=13, leading=18,
                        textColor=colors.HexColor("#1a5c8a"), spaceBefore=14, spaceAfter=6)
    body = ParagraphStyle("body", fontName="DejaVu", fontSize=10, leading=15,
                          textColor=colors.HexColor("#2d3436"))
    small = ParagraphStyle("small", fontName="DejaVu", fontSize=8.5, leading=12,
                           textColor=colors.HexColor("#6b7280"))

    def kv_table(pairs: list[tuple[str, Any]]) -> Table:
        data = [[Paragraph(f"<b>{k}</b>", body), Paragraph(str(v or "—"), body)] for k, v in pairs]
        t = Table(data, colWidths=[6 * cm, 10.5 * cm])
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f8f9fc")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    doc = SimpleDocTemplate(
        str(pdf_path), pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.8 * cm, bottomMargin=1.8 * cm,
        title="АЛТ — Анализ процессов предприятия",
    )
    story = [
        Paragraph("АЛТ — Альтернативные Легальные Технологии", small),
        Paragraph("Анализ процессов предприятия", h1),
        Paragraph(
            f"Отчёт №{diag_id} · {datetime.now().strftime('%d.%m.%Y')} · "
            f"{company or '—'} · {name or '—'}", body),
        Spacer(1, 8),
        kv_table([("Заказчик", name), ("Организация", company), ("Email", email),
                  ("Стоимость", "3 000 ₽ — оплачено")]),
        Paragraph("1. Уровень зрелости процессов", h2),
        Paragraph(
            "Оценка по 5-балльной шкале формируется аналитиком по ответам ниже. "
            "Предварительный уровень и рекомендации — в разделах 2–6.", body),
        Paragraph("2. Коммуникации", h2),
        kv_table([("Мессенджеры", answers.get("messengers")),
                  ("Диалогов в день", answers.get("dialogs_per_day")),
                  ("Скрипты / CRM", answers.get("scripts_crm"))]),
        Paragraph("3. ИТ-инфраструктура", h2),
        kv_table([("Серверы", answers.get("servers")),
                  ("Домены / почта", answers.get("domains")),
                  ("SaaS и ИИ-инструменты", answers.get("saas"))]),
        Paragraph("4. Комплаенс (152-ФЗ / 168-ФЗ)", h2),
        kv_table([("Категории ПДн", answers.get("pdn_categories")),
                  ("Уведомление РКН", answers.get("rkn")),
                  ("Согласия / договор", answers.get("pdn_consent")),
                  ("Интернет-реклама", answers.get("ads")),
                  ("ОРД", answers.get("ord"))]),
        Paragraph("5. ИИ и автоматизация — потенциал", h2),
        Paragraph(
            "На основе разделов 2–3 аналитик АЛТ определит процессы-кандидаты на автоматизацию: "
            "типовые ответы клиентам, сбор заявок, отчётность. Возможные продукты: "
            "ВежПом (мониторинг), НейроСотрудник (ИИ-ассистент), АЛТ-эксперт (консультант).", body),
        Paragraph("6. Дорожная карта (1 / 3 / 6 месяцев)", h2),
        kv_table([("Задачи на 6 мес.", answers.get("goals")),
                  ("Бюджет и сроки", answers.get("budget"))]),
        Paragraph(
            "1 мес. — устранение критичных рисков комплаенса; "
            "3 мес. — миграция коммуникаций в MAX/VK, внедрение ИИ-ассистента; "
            "6 мес. — автономные процессы и измеримая экономия времени.", body),
        Spacer(1, 12),
        Paragraph(
            "Этот отчёт подготовлен автоматически по анкете №{id}. "
            "Персональную сессию с разбором проводит Шеф АЛТ — v.devops@yandex.ru.".format(id=diag_id),
            small),
    ]
    doc.build(story)
    return str(pdf_path)


def build_free_summary_pdf(diag_id: int, *, name: str, scores: dict[str, Any]) -> str | None:
    """Краткий PDF со шкалами для бесплатной диагностики (опционально)."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        _register_fonts()
        out_dir = config.DATA_DIR / "diagnostics_reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = out_dir / f"summary_{diag_id}.pdf"

        h1 = ParagraphStyle("h1", fontName="DejaVu-Bold", fontSize=16, leading=22,
                            textColor=colors.HexColor("#1a5c8a"), spaceAfter=8)
        body = ParagraphStyle("body", fontName="DejaVu", fontSize=10, leading=15,
                              textColor=colors.HexColor("#2d3436"))
        rows = [[k, f"{v} / 100"] for k, v in scores.get("scales", {}).items()]
        rows.append(["ИТОГО", f"{scores.get('total')} / 100 — {scores.get('level_text', '')}"])
        t = Table([[Paragraph(f"<b>{a}</b>", body), Paragraph(b, body)] for a, b in rows],
                  colWidths=[10 * cm, 6 * cm])
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f8f9fc")]),
        ]))
        doc = SimpleDocTemplate(str(pdf_path), pagesize=A4,
                                leftMargin=2 * cm, rightMargin=2 * cm,
                                topMargin=2 * cm, bottomMargin=2 * cm)
        doc.build([
            Paragraph("АЛТ — Бесплатная диагностика", h1),
            Paragraph(f"Заявка №{diag_id} · {name}", body),
            Spacer(1, 10),
            t,
            Spacer(1, 10),
            Paragraph(
                "🟢 0–30 — низкий риск · 🟡 31–60 — средний · 🔴 61–100 — высокий. "
                "Подробные рекомендации — в письме и на консультации: v.devops@yandex.ru", body),
        ])
        return str(pdf_path)
    except Exception:
        return None


__all__ = ["build_paid_report", "build_free_summary_pdf"]
