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

- Python 3.11 or newer installed
- An existing US visa appointment booked on https://ais.usvisa-info.com/en-ca/
- Gmail account for notifications (optional but recommended)

## Installation

1. Clone this repository
2. Install dependencies:

```sh
pip install -r requirements.txt
```

This installs the Node package in editable mode. The standalone installation can also be selected explicitly with `pip install -e ".[node]"`; Coordinator dependencies are separate in `.[coordinator]`.

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

Use [.env.example](.env.example) as a template for a new `.env` in the project root. MODE defaults to solo. The existing .env and saved sessions remain in the project root after the package migration. You can use the following parameters:

```
USER_EMAIL=""   # The email address for your https://ais.usvisa-info.com/en-ca/niv/users/sign_in account
USER_PASSWORD=""    # The password for your  https://ais.usvisa-info.com/en-ca/niv/users/sign_in account
NUM_PARTICIPANTS=1
TEST_MODE=true          # false enables real booking
SHOW_GUI=true          # show Chrome during login
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
```

Checks continue on the same authenticated HTTP session until a real booking is
verified or the program is stopped. A dry run keeps polling. The cycle gap replaces
the normal delay after the configured number of dates requests. Cycles reuse the
HTTP session. With cycle length 0, checks run continuously. A suitable date can
trigger further requests for times, the booking form and verification.
Restart the script after changing `.env`.

Every nonempty availability response is appended to `logs/available_dates.log`
with the local timestamp, consulate and full list of dates, before filtering by
the configured range or exclusions. Empty responses are not written to this file.
The log is preserved across restarts; a write failure is reported in the console
and does not interrupt polling.

HTTP 429 responses trigger cooldowns of 4, 8, 16, 32, 64, 128, 256, 512, 1024,
2048 and 4096 seconds. A further 429 sends a Gmail notification and stops the
program with exit code 1. `Retry-After` seconds or HTTP dates can extend a cooldown.
Backoff persists across reauthentication and resets after an availability check
without throttling. Booking POSTs are never automatically retried.
Empty dates use the ordinary polling delay and cycle gap, without exponential
backoff or reauthentication. The HTTP 429 backoff restarts when the program
restarts. Errors and empty responses count
toward cycles; at a cycle boundary the longer applicable pause is used, without
adding the cycle gap to backoff.
The HTTP 429 backoff is enabled only when all three of its parameters are present and
positive. Setting any parameter to `0`, leaving it blank or omitting it disables
that backoff, including the stop after exhausted HTTP 429 cooldowns. A disabled
backoff uses the ordinary polling delay and cycle gap; HTTP 429 still respects
`Retry-After`. Initial and maximum delays are in seconds; multipliers are unitless.
Expired authentication restarts Selenium login and resumes polling.

With `PERSIST_SESSION=true` (default), cookies and the appointment URL are saved
per account in `.sessions/` (ignored by Git). Valid sessions skip Selenium on
later runs; expired sessions log in again. Set `PERSIST_SESSION=false` to disable.

Gmail is optional. Set the sender address in `GMAIL_EMAIL`, a
[Google App Password](https://support.google.com/accounts/answer/185833) in
`GMAIL_APPLICATION_PWD`, and the recipient in `RECEIVER_EMAIL`. Check it separately
with `python tests/integration/test_check_gmail.py`; this sends one real test email.

Unit tests live in `tests/unit` and use mocked portal/SMTP responses. Install the application extras for the full suite, then run:

```sh
python -m pip install -e ".[node,coordinated,coordinator]"
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

## Coordinated mode

The [setup guide](docs/coordinated-setup.md) covers the working Node/Coordinator system and all three connection options: Tailscale, a temporary Cloudflare Tunnel, or Cloudflare Tunnel on your own domain. The [architecture and API contract](docs/coordinated-mode-plan.md) describe the protocol and delivery guarantees.

Both `solo` and `coordinated` monitoring modes are implemented. Solo remains the default. The compatible `python reschedule.py` launcher remains available, and an editable installation also provides `usvisa-node --mode solo`, `usvisa-node --mode coordinated`, and `python -m usvisa.node`.

To try the server without your own domain (Docker required):

```sh
python scripts/server.py setup --profile cloudflare_quick
python scripts/server.py up --server-only
python scripts/server.py create-user --username admin
```

Open the displayed HTTPS URL, sign in, create a node, and save its token/configuration. Install `python -m pip install -e ".[node,coordinated]"` on each Node, fill its local visa settings, set `MODE=coordinated`, `SERVER_URL`, `NODE_TOKEN`, and `NODE_MIN_DATE`, then launch it. Each node polls every 180 seconds with evenly spaced phases (90 seconds apart for two nodes). Heartbeats and delivery run independently of the visa worker. Use `TEST_MODE=true` for the first run.

After a verified real booking, the node prints a green uppercase success message and saves a booking stop in its local SQLite state. Coordinated monitoring, heartbeats and sharing continue, while further booking from either local checks or received findings stays disabled across restarts. The booked date is excluded from future findings. Dry runs do not set this stop and keep polling. Solo mode exits after success; an uncertain booking outcome retains the submission guard and stops for manual verification.

For a combined Docker deployment, save `PORTAL_USERNAME`, `PORTAL_PASSWORD`, and `NODE_NAME` in the private `.env` instead of copying a token. Keep `COMPOSE_PROFILES=${MODE:-solo},${MODE:-solo}-${EXPOSE_MODE:-cloudflare_quick}` alongside `MODE` and `EXPOSE_MODE`. Then `docker compose up -d --build` starts the Coordinator and a Chromium-equipped Node, registers it once, persists its token, receives a schedule, and runs the existing visa worker. `MODE=solo` selects only the Node. Use `python scripts/server.py up --mode solo` or `--mode coordinated` to switch modes and stop unused services automatically. The local Node and Coordinator discover changing Quick Tunnel hostnames without Docker socket access; remote nodes still need their `SERVER_URL` updated. See the setup guide for persistent state and first-account setup.

Application code lives in `src/usvisa`: `node` runs the account worker, `coordinator` owns the server entry point, `visa` contains login and HTTP booking, `notifications` delivers email, and `shared` contains paths and audit. Both application entry points support `--help` without importing browser or broker clients.

The `protocol` package validates all five versioned JSON messages. The Coordinator core persists membership, schedule UUID/revision, evenly spaced 180-second phases, deduplication, findings and a transactional outbox in SQLite. Each recipient receives a personal schedule; findings are filtered against the recipient's own `minDate`. Restarting preserves state and pending message IDs. Local commands work without RabbitMQ or visa credentials:

```sh
usvisa-coordinator init-db --consulate-id 95 --city Vancouver
usvisa-coordinator state
```

The default database is `.coordinator/state.sqlite3` under the data directory; pass `--database PATH` before the subcommand to override it. These commands initialize or inspect local state. `usvisa-coordinator serve` starts the portal and AMQP services; the Compose launcher supplies its configuration and persists both SQLite and RabbitMQ volumes. Server dependencies are pinned in `requirements-coordinator.lock`; the portal's compiled Tailwind CSS is included, so deployment does not require npm.

The opt-in integration suites verify a real RabbitMQ restart, WSS delivery through a real Quick Tunnel, queue permissions, validated user IDs, credential rotation, and revocation without contacting the visa site or SMTP. See the setup guide for exact commands, profile switching and backups.

The default .env, .sessions, logs and .booking-state locations stay at the checkout root, independent of the working directory. Set USVISA_ENV_FILE to choose a configuration file and USVISA_DATA_DIR to choose a state directory. Relative overrides resolve from the checkout root; installed packages outside a checkout use the launch directory as their default. Existing booking guards must be retained when changing the state directory.

## Contribution

Please feel free to report issues. PRs are welcomed and greatly appreciated!

Booking now uses `AppointmentClient`; `legacy/legacy_rescheduler.py` is kept for reference.

## Special thanks

Huge thanks to [@jywyq](https://github.com/jywyq) for adding the Gmail notification feature.

Huge thanks to [@bsingh-kpt](https://github.com/bsingh-kpt) for (finally!) fixing the `legacy_rescheduler` in Mar 2025.

Thanks to [@trungnguyen21](https://github.com/trungnguyen21) and [@saroopskesav](https://github.com/saroopskesav) for helping with the consulate numbers in other cities.

## Disclaimer

This script is provided as-is for the purpose of assisting individuals in rescheduling appointments. While it has been developed with care and with the intention of being helpful, it comes with no guarantees or warranties of any kind, either expressed or implied. By using this script, you acknowledge and agree that you are doing so at your own risk. The author(s) or contributor(s) of this script shall not be held liable for any direct or indirect damages that arise from its use. Please ensure that you understand the actions performed by this script before running it, and consider the ethical and legal implications of its use in your context.

### One submission per application and audit trail

Before a real appointment POST, the client exclusively creates and flushes
`.booking-state/<application-id>.json`. This blocks a second submission from
another client/process or after restart using this checkout. The marker remains
after success, rejection, timeout, failed verification or interruption. This is
an **at-most-once submission** guarantee, not a guarantee that the portal will
save a booking. Dry runs never create the marker. Keep this directory when
restarting or moving the installation; independent copies/machines do not share
its protection. Only consider removing a marker after manually checking the
portal and deliberately deciding to permit another submission.

`logs/rescheduler.jsonl` contains UTC timestamps, run IDs and process IDs,
HTTP GET endpoint/status/duration/error type, consent submission events,
availability and filtering, prepared slot and applicant count, booking POST
outcome/duration, verification page matches, notifications, and run exit status.
Cookies, passwords, CSRF tokens, applicant IDs, and portal response bodies are
not recorded. Audit writes are flushed to disk; a write failure stops execution.
The submission marker is kept independently of the audit log.

The flow is: restore session or sign in ? discover application and prepare form
? fetch and filter dates ? fetch fresh form and available times ? choose earliest
time ? dry-run exit, or persist guard and submit once ? verify the saved date,
time and consulate ? notify and exit. Unknown outcomes require manual checking.
