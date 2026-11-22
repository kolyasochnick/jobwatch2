from __future__ import annotations

import html as H
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


def send_email(subject: str, text: str, html_body: str) -> None:
    missing = [k for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_TO") if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Не заданы переменные окружения: {', '.join(missing)}")
    host, user = os.environ["SMTP_HOST"], os.environ["SMTP_USER"]
    port = int(os.getenv("SMTP_PORT") or 465)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("MAIL_FROM") or user
    msg["To"] = os.environ["MAIL_TO"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls(context=ctx)
    with server:
        server.login(user, os.environ["SMTP_PASSWORD"])
        server.send_message(msg)


def build_digest(new_by_company: dict, alerts: list):
    """new_by_company: {компания: [Job]}, alerts: [(компания, число_сбоев, ошибка)]."""
    total = sum(len(v) for v in new_by_company.values())
    if total:
        names = list(new_by_company)
        subject = f"Новые вакансии: {total} ({', '.join(names[:3])}{'…' if len(names) > 3 else ''})"
    else:
        subject = "JobWatch: проблемы с источниками"

    text, html = [], ["<div style='font-family:Arial,sans-serif;font-size:14px'>"]
    for company, jobs in new_by_company.items():
        text.append(f"{company} ({len(jobs)})")
        html.append(f"<h3 style='margin:16px 0 6px'>{H.escape(company)} ({len(jobs)})</h3><ul>")
        for j in jobs:
            loc = f" — {j.location}" if j.location else ""
            text.append(f"  • {j.title}{loc}\n    {j.url}")
            html.append(
                f"<li><a href='{H.escape(j.url, quote=True)}'>{H.escape(j.title)}</a>"
                f"{H.escape(loc)}</li>"
            )
        html.append("</ul>")
        text.append("")

    if alerts:
        text.append("Проблемы с источниками:")
        html.append("<h3 style='margin:16px 0 6px;color:#b00020'>Проблемы с источниками</h3><ul>")
        for name, fails, err in alerts:
            line = f"«{name}» не проверяется уже {fails} раз(а) подряд: {err}"
            text.append(f"  • {line}")
            html.append(f"<li>{H.escape(line)}</li>")
        html.append("</ul>")
    html.append("</div>")
    return subject, "\n".join(text), "".join(html)
