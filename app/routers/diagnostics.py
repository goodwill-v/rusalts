"""API диагностик (ТЗ 5.4.3): POST /api/diagnostics/{free,your-business,paid}.

Email после отправки — заглушка (print), PDF платной — заглушка (txt-файл).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.diagnostics_store import save_diagnostic, save_pdf_stub
from app.observability import json_log

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

KIND_TITLES = {
    "free": "Бесплатная диагностика",
    "your_business": "Ваше дело",
    "paid": "Анализ процессов предприятия",
}


def _email_stub(kind: str, diag_id: int, email: str, result_note: str) -> None:
    """Заглушка отправки email (далее — SMTP)."""
    print(
        f"[DIAGNOSTICS EMAIL] kind={kind} id={diag_id} to={email} "
        f"subject=АЛТ: {KIND_TITLES.get(kind, kind)} — результат :: {result_note}"
    )


class DiagnosticSubmission(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=50)
    company: str = Field(default="", max_length=300)
    answers: dict = Field(default_factory=dict)


def _submit(kind: str, body: DiagnosticSubmission) -> JSONResponse:
    email = (body.email or "").strip()
    if "@" not in email or not body.name.strip():
        return JSONResponse(
            {"ok": False, "error": "Имя и корректный email обязательны"}, status_code=400
        )
    diag_id = save_diagnostic(
        kind,
        name=body.name.strip(),
        email=email,
        phone=body.phone.strip(),
        company=body.company.strip(),
        answers=body.answers,
    )
    result_note = "чек-лист и рекомендации — следующим письмом"
    if kind == "paid":
        path = save_pdf_stub(diag_id)
        result_note = f"PDF-отчёт (заглушка): {path}"
    _email_stub(kind, diag_id, email, result_note)
    rid = uuid.uuid4().hex
    json_log(
        {
            "type": "diagnostics_submitted",
            "request_id": rid,
            "diagnostic_id": diag_id,
            "kind": kind,
        }
    )
    return JSONResponse(
        {
            "ok": True,
            "diagnostic_id": diag_id,
            "kind": kind,
            "request_id": rid,
            "message": (
                "Спасибо! Результат придёт на указанный email."
                if kind != "paid"
                else "Спасибо! PDF-отчёт будет отправлен на указанный email."
            ),
        }
    )


@router.post("/free")
async def submit_free(body: DiagnosticSubmission) -> JSONResponse:
    return _submit("free", body)


@router.post("/your-business")
async def submit_your_business(body: DiagnosticSubmission) -> JSONResponse:
    return _submit("your_business", body)


@router.post("/paid")
async def submit_paid(body: DiagnosticSubmission) -> JSONResponse:
    return _submit("paid", body)
