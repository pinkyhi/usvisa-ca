"""Optional Gmail notifications; a mail failure never changes a booking result."""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

import settings


def send_notification(subject, text):
    if not all((settings.GMAIL_EMAIL, settings.GMAIL_APPLICATION_PWD, settings.RECEIVER_EMAIL)):
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.GMAIL_SENDER_NAME, settings.GMAIL_EMAIL))
    message["To"] = formataddr((settings.RECEIVER_NAME, settings.RECEIVER_EMAIL))
    message.set_content(text)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=settings.HTTP_TIMEOUT) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(settings.GMAIL_EMAIL, settings.GMAIL_APPLICATION_PWD.replace(" ", ""))
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as error:
        logging.getLogger(__name__).warning(
            "Gmail notification failed (%s); the booking result is unchanged",
            type(error).__name__,
        )
        return False
    return True
