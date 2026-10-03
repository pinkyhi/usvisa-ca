from datetime import date, datetime
from time import sleep

from selenium import webdriver
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from appointment_client import (
    AppointmentClient, AuthenticationExpired, BookingNotVerified, PortalError, RateLimited,
    acceptable_dates,
)
from notifications import send_notification
from session_cache import SessionCache
from settings import *


def log_message(message: str) -> None:
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}")

def get_chrome_driver() -> WebDriver:
    options = webdriver.ChromeOptions()
    if not SHOW_GUI:
        options.add_argument("--headless=new")
        options.add_argument("window-size=1920x1080")
        options.add_argument("disable-gpu")
    options.add_experimental_option("detach", DETACH)
    options.add_argument('--incognito')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    driver = webdriver.Chrome(options=options)
    return driver


def login(driver: WebDriver) -> None:
    driver.get(LOGIN_URL)
    timeout = TIMEOUT

    email_input = WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.ID, "user_email"))
    )
    email_input.send_keys(USER_EMAIL)

    password_input = WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.ID, "user_password"))
    )
    password_input.send_keys(USER_PASSWORD)

    policy_checkbox = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.CLASS_NAME, "icheckbox"))
    )
    policy_checkbox.click()

    login_button = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.NAME, "commit"))
    )
    login_button.click()
    WebDriverWait(driver, timeout).until(
        EC.invisibility_of_element_located((By.ID, "user_email")),
        message="Login did not complete; check the visa account credentials",
    )
    WebDriverWait(driver, timeout).until(
        lambda current: "/users/sign_in" not in current.current_url,
        message="Login did not redirect to the account page",
    )


def get_available_dates(client: AppointmentClient):
    try:
        return client.get_available_dates()
    except (AuthenticationExpired, RateLimited):
        raise
    except PortalError as error:
        log_message(str(error))
        return None


class RateLimitExhausted(PortalError):
    """All rate-limit cooldowns were used without recovery."""


class RateLimitBackoff:
    def __init__(self):
        self.empty_delay = EMPTY_DATES_BACKOFF_INITIAL_DELAY
        self.reset()

    def reset(self):
        self.delay = RATE_LIMIT_BACKOFF_INITIAL_DELAY

    def wait(self, error, minimum_delay=0):
        if self.delay > RATE_LIMIT_BACKOFF_MAX_DELAY:
            raise RateLimitExhausted(
                f"Portal still returns HTTP 429 after cooldowns from "
                f"{RATE_LIMIT_BACKOFF_INITIAL_DELAY} to {RATE_LIMIT_BACKOFF_MAX_DELAY} seconds; stopping."
            )
        delay = max(self.delay, error.retry_after, minimum_delay)
        self.delay = (RATE_LIMIT_BACKOFF_MAX_DELAY + 1
                      if self.delay == RATE_LIMIT_BACKOFF_MAX_DELAY
                      else min(self.delay * RATE_LIMIT_BACKOFF_MULTIPLIER, RATE_LIMIT_BACKOFF_MAX_DELAY))
        log_message(f"Rate limited; waiting {delay:g} seconds before another request")
        sleep(delay)

    def wait_empty(self, minimum_delay=0):
        delay = max(self.empty_delay, minimum_delay)
        self.empty_delay = min(self.empty_delay * EMPTY_DATES_BACKOFF_MULTIPLIER,
                               EMPTY_DATES_BACKOFF_MAX_DELAY)
        log_message(f"Empty dates; waiting {delay:g} seconds before another request")
        sleep(delay)


def reschedule(client: AppointmentClient, backoff=None):
    backoff = backoff if backoff is not None else RateLimitBackoff()
    checks_in_cycle = 0

    def wait_for_next_check(reason=None):
        nonlocal checks_in_cycle
        delay = DATE_REQUEST_DELAY if reason is None else 0
        if DATE_REQUEST_CYCLE_LENGTH and checks_in_cycle >= DATE_REQUEST_CYCLE_LENGTH:
            checks_in_cycle = 0
            delay = DATE_REQUEST_CYCLE_GAP
            log_message(f"Date request cycle finished; next cycle in {delay} seconds")
        if reason == "empty":
            backoff.wait_empty(minimum_delay=delay)
        elif reason is not None:
            backoff.wait(reason, minimum_delay=delay)
        else:
            sleep(delay)
    earliest = date.fromisoformat(EARLIEST_ACCEPTABLE_DATE)
    latest = date.fromisoformat(LATEST_ACCEPTABLE_DATE)
    exclusions = [(date.fromisoformat(start), date.fromisoformat(end))
                  for start, end in EXCLUSION_DATE_RANGES]
    while True:
        checks_in_cycle += 1
        try:
            dates = get_available_dates(client)
        except RateLimited as error:
            if RUN_ONCE:
                raise
            wait_for_next_check(error)
            continue
        if dates is None:
            if RUN_ONCE:
                return None
            wait_for_next_check()
            continue
        if not dates:
            backoff.reset()
            log_message("Portal returned HTTP 200 with no available dates; rate limiting cannot be determined from an empty list alone")
            if RUN_ONCE:
                return None
            wait_for_next_check("empty")
            continue
        candidates = acceptable_dates(dates, earliest, latest, exclusions)
        if not candidates:
            log_message(f"No dates match the configured range/exclusions; earliest available: {dates[0]}")
        rate_limited = False
        for day in candidates:
            log_message(f"Checking appointment times on {day}")
            try:
                result = client.book(day, dry_run=TEST_MODE)
            except RateLimited as error:
                if RUN_ONCE:
                    raise
                wait_for_next_check(error)
                rate_limited = True
                break
            except (AuthenticationExpired, BookingNotVerified):
                raise
            except PortalError as error:
                log_message(f"Could not prepare booking: {error}")
                break
            if result is None:
                log_message(f"No available times on {day}; checking the next suitable date")
                continue
            if result.dry_run:
                log_message(
                    f"TEST MODE: would book {result.date} at {result.time} in "
                    f"{USER_CONSULATE}. No booking POST was sent."
                )
                send_notification(
                    f"[TEST] Visa slot found for {result.date}",
                    f"Available slot: {result.date} at {result.time}, {USER_CONSULATE}. "
                    "The booking form was prepared, but no appointment was changed.",
                )
            else:
                log_message(
                    f"VERIFIED: appointment booked for {result.date} at "
                    f"{result.time} in {USER_CONSULATE}"
                )
                send_notification(
                    f"Visa Appointment Rescheduled for {result.date}",
                    f"Your appointment has been verified on the portal for "
                    f"{result.date} at {result.time}, {USER_CONSULATE}.",
                )
            return result
        if rate_limited:
            continue
        backoff.reset()
        if RUN_ONCE:
            return None
        wait_for_next_check()


def close_driver(driver):
    try:
        driver.quit()
    except WebDriverException as error:
        log_message(f"Browser cleanup failed ({type(error).__name__})")


def run_http_session(client, cache=None, validate_session=False, backoff=None):
    session_valid = True
    try:
        if validate_session:
            client.check_session()
            log_message("Saved login session is valid; Selenium login was skipped")
        if backoff is None:
            return reschedule(client)
        return reschedule(client, backoff=backoff)
    except AuthenticationExpired:
        session_valid = False
        if cache is not None:
            cache.invalidate()
        raise
    finally:
        if cache is not None and session_valid:
            # Save the latest HTTP cookies, not just those originally from Chrome.
            cache.save(client)


def reschedule_with_new_session(backoff=None):
    cache = SessionCache(SESSION_CACHE_DIR, USER_EMAIL) if PERSIST_SESSION else None
    if cache is not None:
        client = cache.load(CONSULATES[USER_CONSULATE], USER_CONSULATE, HTTP_TIMEOUT)
        if client is not None:
            try:
                with client:
                    return run_http_session(client, cache, validate_session=True, backoff=backoff)
            except AuthenticationExpired:
                if RUN_ONCE:
                    raise
                log_message("Saved login session expired; logging in again with Selenium")
    driver = get_chrome_driver()
    try:
        for attempt in range(NEW_SESSION_AFTER_FAILURES):
            try:
                login(driver)
                break
            except WebDriverException as error:
                log_message(f"Could not sign in ({type(error).__name__})")
                if attempt + 1 == NEW_SESSION_AFTER_FAILURES:
                    raise PortalError("Selenium could not sign in after all login attempts") from error
                sleep(FAIL_RETRY_DELAY)
        with AppointmentClient.from_driver(
            driver, CONSULATES[USER_CONSULATE], USER_CONSULATE, HTTP_TIMEOUT,
        ) as client:
            # The HTTP session owns its cookies now. Close Chrome to stop its
            # background requests while polling/bookings proceed over HTTP.
            close_driver(driver)
            driver = None
            log_message("Selenium sign-in completed; preparing appointment through requests")
            setup_backoff = backoff if backoff is not None else RateLimitBackoff()
            while True:
                try:
                    client.prepare_session()
                    break
                except RateLimited as error:
                    if RUN_ONCE:
                        raise
                    setup_backoff.wait(error)
            return run_http_session(client, cache, backoff=backoff)
    finally:
        if driver is not None:
            close_driver(driver)


def main():
    try:
        validate_settings()
    except ValueError as error:
        log_message(str(error))
        return 2
    log_message(f"Consulate: {USER_CONSULATE}")
    log_message(f"Acceptable dates: {EARLIEST_ACCEPTABLE_DATE} to {LATEST_ACCEPTABLE_DATE}")
    log_message(f"TEST_MODE={TEST_MODE}; RUN_ONCE={RUN_ONCE}")
    gmail_enabled = all((GMAIL_EMAIL, GMAIL_APPLICATION_PWD, RECEIVER_EMAIL))
    log_message(f"Gmail notifications: {'enabled' if gmail_enabled else 'disabled'}")
    backoff = RateLimitBackoff()
    try:
        while True:
            log_message("Preparing authenticated session")
            session_failed = False
            try:
                result = reschedule_with_new_session(backoff=backoff)
            except RateLimitExhausted:
                raise
            except RateLimited as error:
                if RUN_ONCE:
                    log_message(str(error))
                    return 1
                backoff.wait(error)
                continue
            except BookingNotVerified as error:
                log_message(str(error))
                send_notification("Visa booking needs manual verification", str(error))
                return 1
            except AuthenticationExpired as error:
                log_message(str(error))
                if RUN_ONCE:
                    return 1
                log_message("Session expired; restarting Selenium login")
                continue
            except (PortalError, WebDriverException) as error:
                log_message(f"Session failed: {error}")
                session_failed = True
                result = None
            if result is not None:
                return 0
            if RUN_ONCE:
                log_message("Single availability check finished without a suitable slot")
                return 1 if session_failed else 0
            sleep(DATE_REQUEST_DELAY)
    except RateLimitExhausted as error:
        log_message(str(error))
        delivered = send_notification("Visa rescheduler stopped: rate limit", str(error))
        if not delivered:
            log_message("Rate-limit email could not be sent; check Gmail notification settings and logs")
        return 1
    except KeyboardInterrupt:
        log_message("Stopped by user")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
