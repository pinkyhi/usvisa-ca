import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv


# Always use the .env beside this file, including when launched from an IDE.
# Explicit environment variables take precedence over the file.
load_dotenv(Path(__file__).with_name(".env"))


def env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower().strip() in {"true", "1", "yes", "on"}:
        return True
    if value.lower().strip() in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


USER_EMAIL = os.getenv("USER_EMAIL", "")
USER_PASSWORD = os.getenv("USER_PASSWORD", "")
NUM_PARTICIPANTS = int(os.getenv("NUM_PARTICIPANTS", "1"))
EARLIEST_ACCEPTABLE_DATE = os.getenv("EARLIEST_ACCEPTABLE_DATE", "")
LATEST_ACCEPTABLE_DATE = os.getenv("LATEST_ACCEPTABLE_DATE", "")
USER_CONSULATE = os.getenv("USER_CONSULATE", "")

CONSULATES = {
    "Calgary": 89,
    "Halifax": 90,
    "Montreal": 91,
    "Ottawa": 92,
    "Quebec": 93,
    "Toronto": 94,
    "Vancouver": 95,
}

EXCLUSION_DATE_RANGES = []
for i in range(1, 10):
    start = os.getenv(f"EXCLUSION_START_DATE_{i}", "")
    end = os.getenv(f"EXCLUSION_END_DATE_{i}", "")
    if start or end:
        EXCLUSION_DATE_RANGES.append((start, end))

GMAIL_SENDER_NAME = os.getenv("GMAIL_SENDER_NAME", "Visa Rescheduler")
GMAIL_EMAIL = os.getenv("GMAIL_EMAIL", "")
GMAIL_APPLICATION_PWD = os.getenv("GMAIL_APPLICATION_PWD", "")
RECEIVER_NAME = os.getenv("RECEIVER_NAME", "")
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL", "")

# A test run logs in and prepares a booking, but never submits the booking POST.
TEST_MODE = env_bool("TEST_MODE", True)
SHOW_GUI = env_bool("SHOW_GUI", False)
RUN_ONCE = env_bool("RUN_ONCE", False)
PERSIST_SESSION = env_bool("PERSIST_SESSION", True)
SESSION_CACHE_DIR = Path(__file__).with_name(".sessions")
DETACH = env_bool("DETACH", False)
NEW_SESSION_AFTER_FAILURES = int(os.getenv("NEW_SESSION_AFTER_FAILURES", "5"))
NEW_SESSION_DELAY = int(os.getenv("NEW_SESSION_DELAY", "300"))
TIMEOUT = int(os.getenv("TIMEOUT", "10"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
FAIL_RETRY_DELAY = int(os.getenv("FAIL_RETRY_DELAY", "180"))
DATE_REQUEST_DELAY = int(os.getenv("DATE_REQUEST_DELAY", "180"))
DATE_REQUEST_MAX_RETRY = int(os.getenv("DATE_REQUEST_MAX_RETRY", "5"))
DATE_REQUEST_MAX_TIME = int(os.getenv("DATE_REQUEST_MAX_TIME", str(15 * 60)))

LOGIN_URL = "https://ais.usvisa-info.com/en-ca/niv/users/sign_in"
APPOINTMENT_PAGE_URL = "https://ais.usvisa-info.com/en-ca/niv/schedule/{id}/appointment"
PAYMENT_PAGE_URL = "https://ais.usvisa-info.com/en-ca/niv/schedule/{id}/payment"
# Retained for the unmaintained scripts in legacy/.
AVAILABLE_DATE_REQUEST_SUFFIX = (
    f"/days/{CONSULATES.get(USER_CONSULATE, '')}.json?appointments[expedite]=false"
)
REQUEST_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def validate_settings():
    missing = [name for name in (
        "USER_EMAIL", "USER_PASSWORD", "USER_CONSULATE",
        "EARLIEST_ACCEPTABLE_DATE", "LATEST_ACCEPTABLE_DATE",
    ) if not globals()[name]]
    if missing:
        raise ValueError("Fill these settings in .env: " + ", ".join(missing))
    if USER_CONSULATE not in CONSULATES:
        raise ValueError("USER_CONSULATE must be one of: " + ", ".join(CONSULATES))
    try:
        earliest = date.fromisoformat(EARLIEST_ACCEPTABLE_DATE)
        latest = date.fromisoformat(LATEST_ACCEPTABLE_DATE)
        exclusions = [(date.fromisoformat(start), date.fromisoformat(end))
                      for start, end in EXCLUSION_DATE_RANGES]
    except ValueError as error:
        raise ValueError("Dates must use YYYY-MM-DD; exclusion ranges need both dates") from error
    if earliest > latest:
        raise ValueError("EARLIEST_ACCEPTABLE_DATE must not be after LATEST_ACCEPTABLE_DATE")
    if any(start > end for start, end in exclusions):
        raise ValueError("Exclusion start dates must not be after their end dates")
    for name in ("NUM_PARTICIPANTS", "NEW_SESSION_AFTER_FAILURES", "TIMEOUT",
                 "HTTP_TIMEOUT", "DATE_REQUEST_MAX_RETRY", "DATE_REQUEST_MAX_TIME"):
        if globals()[name] <= 0:
            raise ValueError(f"{name} must be positive")
    for name in ("NEW_SESSION_DELAY", "FAIL_RETRY_DELAY", "DATE_REQUEST_DELAY"):
        if globals()[name] < 0:
            raise ValueError(f"{name} must not be negative")
