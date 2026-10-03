# US Visa Rescheduler for Canada

A simple Python script for making US visa interview appointments in Canada

## Update

- The core functionality is still working (as of **March 2026**) according to users' report
- Gmail notifications are optional; delivery failures do not affect booking
- Adopt this repo: this project is looking for a new maintainer, open an issue if you'd like to adopt it.

## Features

- Automatically checks for available visa interview slots at your selected consulate
- Supports multiple consulate locations across Canada
- Configurable date ranges for appointment scheduling
- Email notifications when appointments are found or rescheduled
- Support for excluding specific date ranges
- Headless operation mode for unattended running
- Test mode that prepares a booking without submitting it
- Automatic retry mechanism with configurable delays
- Support for multiple applicants in a single appointment

## Prerequisites

- Python 3.x installed
- An existing US visa appointment booked on https://ais.usvisa-info.com/en-ca/
- Gmail account for notifications (optional but recommended)

## Installation

1. Clone this repository
2. Install dependencies:

```sh
pip install -r requirements.txt
```

Supported Consulate locations:

```python
CONSULATES = {
    "Calgary": 89,
    "Halifax": 90,
    "Montreal": 91,
    "Ottawa": 92,
    "Quebec": 93,
    "Toronto": 94,
    "Vancouver": 95
} # Only Toronto and Vancouver consulates are verified
```

Add a new `.env` file to the root of the project, this file will be used to configure parameters for the script. You can use the following parameters:

```
USER_EMAIL=""   # The email address for your https://ais.usvisa-info.com/en-ca/niv/users/sign_in account
USER_PASSWORD=""    # The password for your  https://ais.usvisa-info.com/en-ca/niv/users/sign_in account
NUM_PARTICIPANTS=1
TEST_MODE=true          # false enables real booking
SHOW_GUI=true          # show Chrome during login
RUN_ONCE=true          # perform one availability check and exit
PERSIST_SESSION=true   # reuse login cookies between runs
EARLIEST_ACCEPTABLE_DATE="" # The earliest interview date you are looking for
LATEST_ACCEPTABLE_DATE=""   # The latest acceptable interview date
USER_CONSULATE="" # Use one of the cosulate names from above
GMAIL_SENDER_NAME=""    # Name of sender on email
GMAIL_EMAIL=""  # Sender email account
GMAIL_APPLICATION_PWD=""    # Use the app password you generated for application -- check https://support.google.com/mail/answer/185833?hl=en
RECEIVER_NAME=""    # Recipient name
RECEIVER_EMAIL=""   # Recipient email
EXCLUSION_START_DATE_1=""   # Start date for first excluded date range
EXCLUSION_END_DATE_1=""     # End date for first excluded date range
EXCLUSION_START_DATE_2=""   # Start date for second excluded date range
EXCLUSION_END_DATE_2=""     # End date for second excluded date range
```

You can add upto 9 exclusion date ranges. Each date range to be excluded using the syntax `EXCLUSION_START_DATE_{i}` and `EXCLUSION_END_DATE_{i}` where `i` can be replaced by numbers between 1 to 9.

### Find a slot and book it automatically

```sh
python reschedule.py
```

Selenium only signs in and hands off cookies; Chrome then closes. `requests`
discovers the application, follows setup links and completes consent screens,
checks availability, submits the booking and verifies
the saved date, time and consulate. `TEST_MODE=true` (default) prepares the form
without submitting it; set `TEST_MODE=false` in `.env` for real booking.
An unverified booking stops the script for manual checking.

Configure polling in `.env` (delays are in seconds):

```dotenv
DATE_REQUEST_DELAY=180       # Seconds between ordinary availability checks
DATE_REQUEST_CYCLE_LENGTH=5  # Checks per cycle; 0 disables cycles
DATE_REQUEST_CYCLE_GAP=900   # Seconds between cycles

# HTTP 429: seconds; notify and stop if still throttled after the maximum pause
RATE_LIMIT_BACKOFF_INITIAL_DELAY=4
RATE_LIMIT_BACKOFF_MAX_DELAY=4096
RATE_LIMIT_BACKOFF_MULTIPLIER=2

# HTTP 200 []: seconds; hold at the maximum pause without resetting
EMPTY_DATES_BACKOFF_INITIAL_DELAY=240
EMPTY_DATES_BACKOFF_MAX_DELAY=3840
EMPTY_DATES_BACKOFF_MULTIPLIER=2
```

With `RUN_ONCE=false`, checks continue on the same authenticated HTTP session
until a suitable slot is found or the program is stopped. The cycle gap replaces
the normal delay after the configured number of dates requests. Cycles reuse the
HTTP session. With cycle length 0, checks run continuously. `RUN_ONCE=true` performs
one availability check, with no delay or retry, including on HTTP 429. A suitable
date can trigger further requests for times, the booking form and verification.
Restart the script after changing `.env`.

HTTP 429 responses trigger cooldowns of 4, 8, 16, 32, 64, 128, 256, 512, 1024,
2048 and 4096 seconds. A further 429 sends a Gmail notification and stops the
program with exit code 1. `Retry-After` seconds or HTTP dates can extend a cooldown.
Backoff persists across reauthentication and resets after an availability check
without throttling. Empty HTTP 200 availability alone cannot distinguish no slots
from hidden throttling. Booking POSTs are never automatically retried.
Empty dates trigger separate pauses of 240, 480, 960, 1920 and then 3840 seconds repeatedly.
The empty-response backoff never resets during the process, even after nonempty
replies or reauthentication. Nonempty replies still use ordinary polling delays.
Both backoffs restart when the program restarts. Errors and empty responses count
toward cycles; at a cycle boundary the longer applicable pause is used, without
adding the cycle gap to backoff. `RUN_ONCE` never waits or retries.
Each backoff is enabled only when all three of its parameters are present and
positive. Setting any parameter to `0`, leaving it blank or omitting it disables
that backoff, including the stop after exhausted HTTP 429 cooldowns. Disabled
backoffs use the ordinary polling delay and cycle gap; HTTP 429 still respects
`Retry-After`. Initial and maximum delays are in seconds; multipliers are unitless.
Expired authentication restarts Selenium login with `RUN_ONCE=false`; with
`RUN_ONCE=true`, an expired session ends the run instead.

With `PERSIST_SESSION=true` (default), cookies and the appointment URL are saved
per account in `.sessions/` (ignored by Git). Valid sessions skip Selenium on
later runs; expired sessions log in again. Set `PERSIST_SESSION=false` to disable.

Gmail is optional. Set the sender address in `GMAIL_EMAIL`, a
[Google App Password](https://support.google.com/accounts/answer/185833) in
`GMAIL_APPLICATION_PWD`, and the recipient in `RECEIVER_EMAIL`. Check it separately
with `python tests/integration/test_check_gmail.py`; this sends one real test email.

Unit tests live in `tests/unit` and use mocked portal/SMTP responses. Run them with:

```sh
python -m unittest discover -s tests/unit -t . -v
```

Integration tests live in `tests/integration`. The Gmail test uses the real `.env`
and sends email when run directly. To enable it through unittest discovery, set
`RUN_GMAIL_INTEGRATION=true` and run `python -m unittest discover -s tests/integration -t . -v`.
Ordinary discovery without this flag skips the live test.

Note `detect_and_notify.py` is no longer maintained.

## Caution

It may not always be feasible to reschedule an appointment multiple times. Therefore, it's crucial to use `TEST_MODE = True` for testing purposes and ensure the `LATEST_ACCEPTABLE_DATE` is genuinely acceptable to you.

Consulates other than Toronto and Vancouver are not tested.

## Contribution

Please feel free to report issues. PRs are welcomed and greatly appreciated!

Booking now uses `AppointmentClient`; `legacy_rescheduler.py` is kept for reference.

## Special thanks

Huge thanks to [@jywyq](https://github.com/jywyq) for adding the Gmail notification feature.

Huge thanks to [@bsingh-kpt](https://github.com/bsingh-kpt) for (finally!) fixing the `legacy_rescheduler` in Mar 2025.

Thanks to [@trungnguyen21](https://github.com/trungnguyen21) and [@saroopskesav](https://github.com/saroopskesav) for helping with the consulate numbers in other cities.

## Disclaimer

This script is provided as-is for the purpose of assisting individuals in rescheduling appointments. While it has been developed with care and with the intention of being helpful, it comes with no guarantees or warranties of any kind, either expressed or implied. By using this script, you acknowledge and agree that you are doing so at your own risk. The author(s) or contributor(s) of this script shall not be held liable for any direct or indirect damages that arise from its use. Please ensure that you understand the actions performed by this script before running it, and consider the ethical and legal implications of its use in your context.
