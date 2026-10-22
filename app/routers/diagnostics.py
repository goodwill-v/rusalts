"""API диагностик (ТЗ 5.4.3): POST /api/diagnostics/{free,your-business,paid}.

Пайплайн: сохранение в SQLite (diagnostics.db) → скоринг (interpretation.md)
→ PDF-отчёт (reportlab, для платной) → письма клиенту и Шефу (SMTP).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import config
from app.diagnostics_pdf import build_paid_report
from app.max_notify import format_lead_message, notify_max
from app.diagnostics_scoring import summarize
from app.diagnostics_store import save_diagnostic, set_pdf_path
from app.observability import json_log

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

KIND_TITLES = {
    "free": "Бесплатная диагностика",
    "your_business": "Ваше дело",
    "paid": "Анализ процессов предприятия",
}


class DiagnosticSubmission(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=50)
    company: str = Field(default="", max_length=300)
    answers: dict = Field(default_factory=dict)


def _send_mail_safe(to: str, subject: str, body: str) -> bool:
    """SMTP-отправка; без настроенного SMTP — заглушка в лог (не ломает отправку формы)."""
    try:
        from app.chief_mail import send_email

        send_email(to=to, subject=subject, body=body)
        return True
    except RuntimeError:
        # SMTP не настроен — заглушка (разработка/CI)
        print(f"[DIAGNOSTICS EMAIL STUB] to={to} subject={subject}")
        return False
    except Exception as e:  # сеть/логин — не валим заявку клиента
        print(f"[DIAGNOSTICS EMAIL ERROR] to={to}: {e}")
        return False


def _client_email(kind: str, diag_id: int, name: str, email: str, result: dict, pdf_path: str) -> str:
    title = KIND_TITLES.get(kind, kind)
    lines = [f"{name}, здравствуйте!", "", f"Это отчёт по заявке №{diag_id} «{title}» (сайт АЛТ)."]
    if kind == "free" and result.get("type") == "scores":
        lines += ["", "Ваши баллы (0–100, чем меньше — тем лучше):"]
        for scale, v in result["scales"].items():
            lines.append(f"  • {scale}: {v}")
        lines.append(f"  ИТОГО: {result['total']} — {result['level_text']}")
        lines += [
            "",
            "🟢 0–30 — низкий риск · 🟡 31–60 — средний · 🔴 61–100 — высокий.",
            "Подробные рекомендации обсудим на бесплатной консультации:",
            "  v.devops@yandex.ru · https://rusalts.ru/consultant",
        ]
    elif kind == "your_business" and result.get("type") == "triggers":
        lines += ["", "Чек-лист под вашу ситуацию:"]
        for t in result["items"]:
            lines.append(f"  • {t['recommendation']} — {t['description']}")
        lines += ["", "Начните с бесплатной консультации: https://rusalts.ru/consultant"]
    elif kind == "paid":
        lines += [
            "",
            "PDF-отчёт «Анализ процессов предприятия» прилагаем к письму",
            "(а также доступен в вашем личном взаимодействии с АЛТ).",
            "Разбор отчёта: v.devops@yandex.ru",
        ]
    lines += ["", "--", "АЛТ — Альтернативные Легальные Технологии", "https://rusalts.ru"]
    return "\n".join(lines)


def _chief_email(kind: str, diag_id: int, name: str, email: str, phone: str, company: str,
                 result: dict, pdf_path: str) -> str:
    return "\n".join([
        f"Новая заявка №{diag_id}: {KIND_TITLES.get(kind, kind)}",
        f"Кто: {name} · {company or '—'} · {email} · {phone or '—'}",
        f"Скоринг/триггеры: {result}",
        f"PDF: {pdf_path or '—'}",
        f"БД: sqlite3 data/diagnostics.db \"SELECT * FROM diagnostics WHERE id={diag_id};\"",
    ])


def _lead_summary(result: dict) -> str:
    """Краткая строка о результате для MAX-уведомления."""
    if result.get("type") == "scores":
        return f"{result.get('level_text', '')} · {result.get('total', '?')}/100"
    if result.get("type") == "triggers":
        recs = [t.get("recommendation", "") for t in result.get("items", [])[:3]]
        return "; ".join(recs) if recs else "чек-лист отправлен"
    return "платная анкета, PDF"


async def _submit(kind: str, body: DiagnosticSubmission) -> JSONResponse:
    email = (body.email or "").strip()
    if "@" not in email or not body.name.strip():
        return JSONResponse(
            {"ok": False, "error": "Имя и корректный email обязательны"}, status_code=400
        )
    result = summarize(kind, body.answers)
    diag_id = save_diagnostic(
        kind,
        name=body.name.strip(),
        email=email,
        phone=body.phone.strip(),
        company=body.company.strip(),
        answers=body.answers,
        result=result,
    )
    pdf_path = ""
    if kind == "paid":
        try:
            pdf_path = build_paid_report(
                diag_id, name=body.name.strip(), company=body.company.strip(),
                email=email, answers=body.answers,
            )
            set_pdf_path(diag_id, pdf_path)
        except Exception as e:
            print(f"[DIAGNOSTICS PDF ERROR] id={diag_id}: {e}")

    _send_mail_safe(
        email,
        f"АЛТ: {KIND_TITLES.get(kind, kind)} — результат (заявка №{diag_id})",
        _client_email(kind, diag_id, body.name.strip(), email, result, pdf_path),
    )
    _send_mail_safe(
        config.CHIEF_EMAIL_TO,
        f"[АЛТ заявка №{diag_id}] {KIND_TITLES.get(kind, kind)}",
        _chief_email(kind, diag_id, body.name.strip(), email, body.phone.strip(),
                     body.company.strip(), result, pdf_path),
    )
    try:
        summary = _lead_summary(result)
        await notify_max(
            format_lead_message(
                diag_id, KIND_TITLES.get(kind, kind), body.name.strip(),
                email + (f" · {body.phone.strip()}" if body.phone.strip() else ""),
                summary,
            )
        )
    except Exception as e:  # MAX-дубль не должен ломать отправку анкеты
        print(f"[DIAGNOSTICS MAX NOTIFY ERROR] id={diag_id}: {e}")

    rid = uuid.uuid4().hex
    json_log(
        {
            "type": "diagnostics_submitted",
            "request_id": rid,
            "diagnostic_id": diag_id,
            "kind": kind,
            "pdf": bool(pdf_path),
        }
    )
    return JSONResponse(
        {
            "ok": True,
            "diagnostic_id": diag_id,
            "kind": kind,
            "request_id": rid,
            "result": result,
            "message": "Спасибо! Результат отправлен на указанный email.",
        }
    )


@router.post("/free")
async def submit_free(body: DiagnosticSubmission) -> JSONResponse:
    return await _submit("free", body)


@router.post("/your-business")
async def submit_your_business(body: DiagnosticSubmission) -> JSONResponse:
    return await _submit("your_business", body)


@router.post("/paid")
async def submit_paid(body: DiagnosticSubmission) -> JSONResponse:
    return await _submit("paid", body)
