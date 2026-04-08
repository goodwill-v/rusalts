from __future__ import annotations

import email
import imaplib
import re
import smtplib
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Literal

from app import config


DecisionKind = Literal["approve", "reject", "edit"]


@dataclass(frozen=True)
class ChiefDecision:
    publication_id: str
    kind: DecisionKind
    raw_text: str
    extracted_text: str | None
    explanation: str | None
    message_id: str | None


_PUB_ID_RE = re.compile(r"\((\d{5})\)")


def _decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_text_message(msg: email.message.Message) -> str:
    if msg.is_multipart():
        parts: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if ctype in ("text/plain", "text/html") and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    text = payload.decode(charset, errors="replace")
                except Exception:
                    text = payload.decode("utf-8", errors="replace")
                parts.append(text)
        return "\n\n".join(p.strip() for p in parts if p and p.strip()).strip()

    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace").strip()
    except Exception:
        return payload.decode("utf-8", errors="replace").strip()


def send_to_chief(*, subject: str, body: str) -> None:
    if not config.SMTP_HOST or not config.SMTP_USER or not config.SMTP_PASSWORD:
        raise RuntimeError("SMTP не настроен: заполните SMTP_HOST/SMTP_USER/SMTP_PASSWORD в .env")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = config.CHIEF_EMAIL_TO
    msg["From"] = (config.SMTP_FROM or config.SMTP_USER)
    msg.set_content(body)

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
        if config.SMTP_TLS:
            s.starttls()
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)


def _parse_decision_text(text: str) -> tuple[DecisionKind, str | None, str | None] | None:
    t = (text or "").strip()
    if not t:
        return None
    upper = t.upper()
    if upper.startswith("ДА"):
        extracted = t.split(":", 1)[1].strip() if ":" in t else None
        return ("approve", extracted or None, None)
    if upper.startswith("НЕТ"):
        extracted = t.split(":", 1)[1].strip() if ":" in t else None
        return ("reject", extracted or None, None)
    if upper.startswith("РЕДАКТИРОВАТЬ"):
        # Formats:
        # - "РЕДАКТИРОВАТЬ, пояснение: <...>"
        # - "РЕДАКТИРОВАТЬ: <...>"
        explanation = None
        extracted = None
        if "пояснение" in upper:
            # split at first colon after "пояснение"
            m = re.search(r"пояснение\s*:\s*(.*)$", t, flags=re.IGNORECASE | re.DOTALL)
            if m:
                explanation = m.group(1).strip() or None
        if ":" in t:
            extracted = t.split(":", 1)[1].strip() or None
        return ("edit", extracted, explanation)
    return None


def _extract_publication_id(subject: str, body: str) -> str | None:
    for s in (subject or "", body or ""):
        m = _PUB_ID_RE.search(s)
        if m:
            return m.group(1)
    return None


def poll_chief_inbox(*, limit: int = 20) -> list[ChiefDecision]:
    if not config.IMAP_HOST or not config.IMAP_USER or not config.IMAP_PASSWORD:
        raise RuntimeError("IMAP не настроен: заполните IMAP_HOST/IMAP_USER/IMAP_PASSWORD в .env")

    out: list[ChiefDecision] = []
    with imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT) as imap:
        imap.login(config.IMAP_USER, config.IMAP_PASSWORD)
        imap.select(config.IMAP_FOLDER)

        typ, data = imap.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()
        ids = ids[-limit:]

        for msg_id in ids:
            typ, msg_data = imap.fetch(msg_id, "(RFC822)")
            if typ != "OK" or not msg_data:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            from_name, from_addr = parseaddr(msg.get("From") or "")
            _ = from_name
            if config.CHIEF_EMAIL_TO and from_addr.lower().strip() != config.CHIEF_EMAIL_TO.lower().strip():
                # mark seen to avoid endless re-processing non-chief messages
                imap.store(msg_id, "+FLAGS", "\\Seen")
                continue

            subject = _decode_mime_header(msg.get("Subject"))
            body = _extract_text_message(msg)
            pub_id = _extract_publication_id(subject, body)

            # decision can be in subject or first non-empty line of body
            candidate_lines: list[str] = []
            if subject:
                candidate_lines.append(subject.strip())
            for line in (body or "").splitlines():
                if line.strip():
                    candidate_lines.append(line.strip())
                    break

            parsed = None
            for cand in candidate_lines:
                parsed = _parse_decision_text(cand)
                if parsed:
                    break

            if pub_id and parsed:
                kind, extracted_text, explanation = parsed
                out.append(
                    ChiefDecision(
                        publication_id=pub_id,
                        kind=kind,
                        raw_text=body or subject or "",
                        extracted_text=extracted_text,
                        explanation=explanation,
                        message_id=(msg.get("Message-Id") or msg.get("Message-ID")),
                    )
                )

            # Mark as seen regardless; unparseable replies should not block mailbox.
            imap.store(msg_id, "+FLAGS", "\\Seen")

    return out

