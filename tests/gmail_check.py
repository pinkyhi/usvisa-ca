"""Shared Gmail check used by offline tests and the live integration test."""

import re
import smtplib

import notifications
import settings


def check_gmail():
    required = ("GMAIL_EMAIL", "GMAIL_APPLICATION_PWD", "RECEIVER_EMAIL")
    missing = [name for name in required if not getattr(settings, name)]
    if missing:
        print("Fill these settings in .env: " + ", ".join(missing))
        return 2
    for name in ("GMAIL_EMAIL", "RECEIVER_EMAIL"):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", getattr(settings, name)):
            print(f"{name} must contain an email address, not a display name")
            return 2
    try:
        notifications.send_notification(
            "[TEST] US Visa Rescheduler Gmail check",
            "Standalone Gmail notification test for US Visa Rescheduler.",
            raise_on_error=True,
        )
    except smtplib.SMTPAuthenticationError as error:
        print(f"Gmail rejected authentication (SMTP {error.smtp_code}). "
              "Check GMAIL_EMAIL and its Google App Password.")
        return 1
    except smtplib.SMTPRecipientsRefused:
        print("SMTP rejected RECEIVER_EMAIL. Check the recipient address.")
        return 1
    except smtplib.SMTPSenderRefused:
        print("SMTP rejected the sender. Check GMAIL_EMAIL.")
        return 1
    except (smtplib.SMTPException, OSError, ValueError) as error:
        stage = getattr(error, "notification_stage", "communicating with Gmail")
        print(f"Gmail test failed while {stage} ({type(error).__name__}). "
              "Check SMTP connectivity and settings.")
        return 1
    print("Gmail accepted one test email. Check the recipient inbox/spam folder.")
    return 0


