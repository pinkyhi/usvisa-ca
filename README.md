# US Visa Rescheduler for Canada

A simple Python script for making US visa interview appointments in Canada

## Update

- The core functionality is still working (as of **March 2026**) according to users' report
- Gmail notifications are optional; delivery failures do not change the booking result
- Adopt this repo: this project is looking for a new maintainer, open an issue if you'd like to adopt it.

## Features

- Automatically checks for available visa interview slots at your selected consulate
- Supports multiple consulate locations across Canada
- Configurable date ranges for appointment scheduling
- Email notifications when appointments are found or rescheduled
- Support for excluding specific date ranges
- Headless operation mode for unattended running
- Test mode that prepares a booking without sending the booking POST
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

Copy `.env.example` to `.env` in the root of the project and fill in your account,
consulate and date range. On PowerShell:

```powershell
Copy-Item .env.example .env
```

If you already have a `.env`, edit it rather than replacing it. The file is
ignored by Git and contains your credentials as plain text. Settings always load
the `.env` beside `settings.py`; existing environment variables take precedence.

Required settings:

- `USER_EMAIL` and `USER_PASSWORD`: credentials for the visa portal, not Gmail.
- `USER_CONSULATE`: one of the names listed above.
- `EARLIEST_ACCEPTABLE_DATE` and `LATEST_ACCEPTABLE_DATE`: inclusive `YYYY-MM-DD` dates.
- `NUM_PARTICIPANTS`: number of applicants (default `1`).

You can exclude up to 9 inclusive date ranges with `EXCLUSION_START_DATE_1` /
`EXCLUSION_END_DATE_1`, through `_9`. Single-day exclusions are supported.

### Login and booking

```sh
python reschedule.py
```

Selenium opens Chrome, signs in, accepts the portal's policy and navigates through
appointment setup. The authenticated cookies and browser user agent are copied
into a `requests.Session` in memory, then Chrome is closed. Cookies and CSRF tokens
are not written to disk by the application.

`appointment_client.py` obtains dates, available times and a fresh appointment
form over HTTP. It preserves the form's hidden fields and repeated applicant IDs,
sets the chosen consulate/date/time, and submits the form with its CSRF token.
After submitting, it separately reloads the instructions/account summary and
checks the saved date, time and consulate for that schedule. A successful HTTP
status or success message alone is not considered proof.

If the POST times out or the saved appointment cannot be verified, the program
checks the saved appointment and stops with exit code `1` if still unverified.
Check your account before restarting: it does not automatically repeat a POST
whose outcome is uncertain. Accounts requiring an additional, unfilled ASC
appointment are rejected before booking.

### Test mode

- `TEST_MODE=true` (the default): real login and availability/form GETs, but no
  booking POST. A matching slot is logged as a test, and the program exits.
  It does not prove the server would accept a real booking.
- `TEST_MODE=false`: submits the booking and verifies the saved appointment.
- `SHOW_GUI=true`: shows Chrome during login/setup; `false` uses headless Chrome.
- `RUN_ONCE=true`: stops after one browser/HTTP session, even without a matching
  slot. The session can perform up to `DATE_REQUEST_MAX_RETRY` availability polls.
  `false` starts new sessions until a slot is prepared/booked.

The example configuration uses `TEST_MODE=true`, `SHOW_GUI=true`, `RUN_ONCE=true`
for checking an account. Set `TEST_MODE=false` and `RUN_ONCE=false` for continuous
automatic booking. Boolean settings accept `true`/`false` in `.env`.

### Optional Gmail notifications

Enable [2-Step Verification and create a Google app password](https://support.google.com/accounts/answer/185833).
Set `GMAIL_EMAIL`, `GMAIL_APPLICATION_PWD` (the app password, not the normal Gmail
password), and `RECEIVER_EMAIL`. `GMAIL_SENDER_NAME` and `RECEIVER_NAME` are optional
display names; the recipient can use any email provider.

The sender connects to `smtp.gmail.com:587` with STARTTLS. If the three required
email settings are empty/incomplete, email is disabled. Delivery failures are
logged separately and never trigger another booking. Notifications distinguish
`[TEST]` slots from verified bookings; an unverified POST produces a message asking
you to check the account.

Note `detect_and_notify.py` is no longer maintained.

### Offline checks

```sh
python -m unittest discover -s tests -v
```

Tests use synthetic portal responses and mocked HTTP/SMTP transports. They cover
test-mode POST suppression, hidden/applicant fields, expired authentication,
false success responses, ambiguous POST timeouts, exclusions, browser cleanup and
optional email. The current portal's account-specific HTML must still be checked
on a real account; unknown confirmation markup is treated as unverified.

## Caution

It may not always be feasible to reschedule an appointment multiple times. Therefore, it's crucial to use `TEST_MODE = True` for testing purposes and ensure the `LATEST_ACCEPTABLE_DATE` is genuinely acceptable to you.

Consulates other than Toronto and Vancouver are not tested.

## Contribution

Please feel free to report issues. PRs are welcomed and greatly appreciated!

The main script uses `AppointmentClient` for HTTP booking. `legacy_rescheduler.py`
is retained as the historical browser implementation and is not called by it.

## Special thanks

Huge thanks to [@jywyq](https://github.com/jywyq) for adding the Gmail notification feature.

Huge thanks to [@bsingh-kpt](https://github.com/bsingh-kpt) for (finally!) fixing the `legacy_rescheduler` in Mar 2025.

Thanks to [@trungnguyen21](https://github.com/trungnguyen21) and [@saroopskesav](https://github.com/saroopskesav) for helping with the consulate numbers in other cities.

## Disclaimer

This script is provided as-is for the purpose of assisting individuals in rescheduling appointments. While it has been developed with care and with the intention of being helpful, it comes with no guarantees or warranties of any kind, either expressed or implied. By using this script, you acknowledge and agree that you are doing so at your own risk. The author(s) or contributor(s) of this script shall not be held liable for any direct or indirect damages that arise from its use. Please ensure that you understand the actions performed by this script before running it, and consider the ethical and legal implications of its use in your context.
