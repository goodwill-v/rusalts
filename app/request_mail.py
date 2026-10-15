"""Отправка заявок обратной связи (форма «Запросить →»).

Письмо уходит на CHIEF_EMAIL_TO (Яндекс) и дублируется на
REQUEST_EMAIL_CC (akord@bk.ru) — реквизиты SMTP из .env.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app import config


def send_request_email(*, subject: str, body: str) -> None:
    """Отправить заявку на почту Шефа + дубль."""
    if not config.SMTP_HOST or not config.SMTP_USER or not config.SMTP_PASSWORD:
        raise RuntimeError(
            "SMTP не настроен: заполните SMTP_HOST/SMTP_USER/SMTP_PASSWORD в .env"
        )

    from_addr = config.SMTP_FROM or config.SMTP_USER
    recipients = [config.CHIEF_EMAIL_TO]
    if config.REQUEST_EMAIL_CC and config.REQUEST_EMAIL_CC not in recipients:
        recipients.append(config.REQUEST_EMAIL_CC)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = ", ".join(recipients)
    msg["From"] = from_addr
    msg.set_content(body)

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
        if config.SMTP_TLS:
            s.starttls()
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)