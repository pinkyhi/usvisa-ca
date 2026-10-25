"""Optional Gmail notifications; a mail failure never changes a booking result."""

import logging
from usvisa.shared import audit
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from usvisa.node import settings


def send_notification(subject, text, *, raise_on_error=False):
    if not all((settings.GMAIL_EMAIL, settings.GMAIL_APPLICATION_PWD, settings.RECEIVER_EMAIL)):
        if raise_on_error:
            raise ValueError("Configure GMAIL_EMAIL, GMAIL_APPLICATION_PWD and RECEIVER_EMAIL in .env")
        audit.event("notification_skipped", reason="unconfigured")
        return False
    stage = "building the message"
    try:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((settings.GMAIL_SENDER_NAME, settings.GMAIL_EMAIL))
        message["To"] = formataddr((settings.RECEIVER_NAME, settings.RECEIVER_EMAIL))
        message.set_content(text)
        # Avoid smtplib's local reverse-DNS lookup, which can hang independently
        # of the socket timeout. Use its fallback loopback address literal.
        stage = "connecting to smtp.gmail.com:587"
        with smtplib.SMTP(
            "smtp.gmail.com", 587,
            local_hostname="[127.0.0.1]", timeout=settings.HTTP_TIMEOUT,
        ) as smtp:
            stage = "sending the initial EHLO"
            smtp.ehlo()
            stage = "starting TLS"
            smtp.starttls(context=ssl.create_default_context())
            stage = "sending EHLO after TLS"
            smtp.ehlo()
            stage = "authenticating"
            smtp.login(settings.GMAIL_EMAIL, settings.GMAIL_APPLICATION_PWD.replace(" ", ""))
            stage = "submitting the message"
            smtp.send_message(message)
            stage = "closing the SMTP session after submission"
    except (smtplib.SMTPException, OSError, ValueError) as error:
        audit.event("notification_failed", stage=stage, error_type=type(error).__name__)
        error.notification_stage = stage
        if raise_on_error:
            raise
        logging.getLogger(__name__).warning(
            "Gmail notification failed while %s (%s); the booking result is unchanged",
            stage, type(error).__name__,
        )
        return False
    audit.event("notification_delivered")
    return True
