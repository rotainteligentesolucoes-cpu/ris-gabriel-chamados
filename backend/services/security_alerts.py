import os
import smtplib
import threading
from email.message import EmailMessage


ROUTE_ALERT_ROLES = {
    role.strip().lower()
    for role in os.getenv(
        "ROUTE_ALERT_ROLES",
        "coordinator,coordenador,controller,controlador,manager,gerente,admin,operator,operador,operational,operacional",
    ).split(",")
    if role.strip()
}


def route_alert_recipient_emails(user_model, company_id, base_id=None):
    query = user_model.query.filter_by(company_id=company_id, active=True)
    users = query.all()
    recipients = []
    for user in users:
        role = (user.role or "").strip().lower()
        if role not in ROUTE_ALERT_ROLES or not user.email or "@" not in user.email:
            continue
        if base_id and user.base_id and user.base_id != base_id:
            continue
        recipients.append(user.email.strip().lower())
    return sorted(set(recipients))


def _send_message(recipients, subject, body, logger=None):
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM", "").strip()
    if not host or not sender or not recipients:
        if logger:
            logger.warning(
                "Alerta de acesso a rota nao enviado: configure SMTP_HOST/SMTP_FROM e destinatarios operacionais."
            )
        return False

    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in ("1", "true", "yes", "sim")
    use_ssl = os.getenv("SMTP_USE_SSL", "false").strip().lower() in ("1", "true", "yes", "sim")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)

    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    try:
        with smtp_class(host, port, timeout=15) as client:
            if use_tls and not use_ssl:
                client.starttls()
            if username:
                client.login(username, password)
            client.send_message(message)
        if logger:
            logger.info("Alerta de seguranca enviado para %s destinatario(s).", len(recipients))
        return True
    except Exception:
        if logger:
            logger.exception("Falha ao enviar alerta de tentativa de acesso a rota.")
        return False


def send_route_access_alert_async(recipients, subject, body, logger=None):
    thread = threading.Thread(
        target=_send_message,
        args=(list(recipients), subject, body, logger),
        name="ris-route-security-alert",
        daemon=True,
    )
    thread.start()
    return thread
