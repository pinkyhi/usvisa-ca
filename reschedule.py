import re
from datetime import date, datetime
from time import sleep

from selenium import webdriver
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from appointment_client import (
    AppointmentClient, AuthenticationExpired, BookingNotVerified, PortalError,
    acceptable_dates,
)
from notifications import send_notification
from request_tracker import RequestTracker
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


def _find_visible_action(driver: WebDriver, label: str):
    locators = (
        (By.LINK_TEXT, label),
        (By.XPATH, f"//button[normalize-space()='{label}']"),
        (By.XPATH, f"//input[@value='{label}']"),
    )
    for locator in locators:
        for element in driver.find_elements(*locator):
            if element.is_displayed() and element.is_enabled():
                return element
    return False


def _click_action_if_present(
    driver: WebDriver, label: str, timeout: float
) -> bool:
    try:
        action = WebDriverWait(driver, timeout).until(
            lambda current_driver: _find_visible_action(current_driver, label)
        )
    except TimeoutException:
        return False
    action.click()
    sleep(2)
    return True


def _has_visible_element(driver: WebDriver, locator) -> bool:
    return any(
        element.is_displayed()
        for element in driver.find_elements(*locator)
    )


def _appointment_page_state(driver: WebDriver):
    if _has_visible_element(
        driver, (By.ID, "appointments_consulate_appointment_date_input")
    ):
        return "ready"
    if _find_visible_action(driver, "Schedule Appointment"):
        return "schedule"
    if _has_visible_element(driver, (By.CLASS_NAME, "icheckbox")):
        return "policy"
    if _find_visible_action(driver, "Continue"):
        return "continue"
    return False


def _prepare_appointment_page(driver: WebDriver) -> None:
    timeout = TIMEOUT
    for _ in range(5):
        state = WebDriverWait(driver, timeout).until(_appointment_page_state)
        if state == "ready":
            return
        if state in {"schedule", "continue"}:
            label = "Schedule Appointment" if state == "schedule" else "Continue"
            _click_action_if_present(driver, label, timeout)
            continue

        policy_checkbox = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CLASS_NAME, "icheckbox"))
        )
        policy_checkbox.click()
        continue_button = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.NAME, "commit"))
        )
        continue_button.click()

    WebDriverWait(driver, timeout).until(
        lambda current_driver: _appointment_page_state(current_driver) == "ready"
    )


def get_appointment_page(driver: WebDriver) -> None:
    timeout = TIMEOUT

    # Newer flows show a group-action page with this link. Older flows first
    # show a Continue link and then expose Schedule Appointment.
    if not _click_action_if_present(driver, "Schedule Appointment", 2):
        if not _click_action_if_present(driver, "Continue", timeout):
            raise TimeoutException(
                "Could not find either 'Schedule Appointment' or 'Continue'"
            )
        _click_action_if_present(driver, "Schedule Appointment", timeout)

    current_url = driver.current_url
    schedule_match = re.search(r"/schedule/(\d+)", current_url)
    if not schedule_match:
        raise PortalError("Could not find the schedule id after opening the appointment page")

    appointment_url = APPOINTMENT_PAGE_URL.format(id=schedule_match.group(1))
    driver.get(appointment_url)


def get_available_dates(client: AppointmentClient, request_tracker: RequestTracker):
    request_tracker.log_retry()
    request_tracker.retry()
    try:
        return client.get_available_dates()
    except AuthenticationExpired:
        raise
    except PortalError as error:
        log_message(str(error))
        return None


def reschedule(client: AppointmentClient, retryCount: int = 0):
    date_request_tracker = RequestTracker(
        retryCount if retryCount > 0 else DATE_REQUEST_MAX_RETRY,
        DATE_REQUEST_MAX_TIME,
    )
    earliest = date.fromisoformat(EARLIEST_ACCEPTABLE_DATE)
    latest = date.fromisoformat(LATEST_ACCEPTABLE_DATE)
    exclusions = [(date.fromisoformat(start), date.fromisoformat(end))
                  for start, end in EXCLUSION_DATE_RANGES]
    while date_request_tracker.should_retry():
        dates = get_available_dates(client, date_request_tracker)
        if dates is None:
            sleep(DATE_REQUEST_DELAY)
            continue
        if not dates:
            log_message("Portal returned no available dates (an empty list can also mean rate limiting)")
            sleep(DATE_REQUEST_DELAY)
            continue
        candidates = acceptable_dates(dates, earliest, latest, exclusions)
        if not candidates:
            log_message(f"No dates match the configured range/exclusions; earliest available: {dates[0]}")
        for day in candidates:
            log_message(f"Checking appointment times on {day}")
            try:
                result = client.book(day, dry_run=TEST_MODE)
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
        sleep(DATE_REQUEST_DELAY)
    return None


def close_driver(driver):
    try:
        driver.quit()
    except WebDriverException as error:
        log_message(f"Browser cleanup failed ({type(error).__name__})")


def run_http_session(client, retryCount, cache=None, validate_session=False):
    session_valid = True
    try:
        if validate_session:
            client.check_session()
            log_message("Saved login session is valid; Selenium login was skipped")
        return reschedule(client, retryCount)
    except AuthenticationExpired:
        session_valid = False
        if cache is not None:
            cache.invalidate()
        raise
    finally:
        if cache is not None and session_valid:
            # Save the latest HTTP cookies, not just those originally from Chrome.
            cache.save(client)


def reschedule_with_new_session(retryCount: int = DATE_REQUEST_MAX_RETRY):
    cache = SessionCache(SESSION_CACHE_DIR, USER_EMAIL) if PERSIST_SESSION else None
    if cache is not None:
        client = cache.load(CONSULATES[USER_CONSULATE], USER_CONSULATE, HTTP_TIMEOUT)
        if client is not None:
            try:
                with client:
                    return run_http_session(client, retryCount, cache, validate_session=True)
            except AuthenticationExpired:
                log_message("Saved login session expired; logging in again with Selenium")
    driver = get_chrome_driver()
    try:
        for attempt in range(NEW_SESSION_AFTER_FAILURES):
            try:
                login(driver)
                get_appointment_page(driver)
                _prepare_appointment_page(driver)
                break
            except WebDriverException as error:
                log_message(f"Could not prepare appointment page ({type(error).__name__})")
                if attempt + 1 == NEW_SESSION_AFTER_FAILURES:
                    raise PortalError("Selenium could not prepare the appointment page after all login attempts") from error
                sleep(FAIL_RETRY_DELAY)
        with AppointmentClient.from_driver(
            driver, CONSULATES[USER_CONSULATE], USER_CONSULATE, HTTP_TIMEOUT,
        ) as client:
            # The HTTP session owns its cookies now. Close Chrome to stop its
            # background requests while polling/bookings proceed over HTTP.
            close_driver(driver)
            driver = None
            log_message("Selenium login completed; availability and booking now use requests")
            return run_http_session(client, retryCount, cache)
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
    session_count = 0
    try:
        while True:
            session_count += 1
            log_message(f"Attempting with new session #{session_count}")
            session_failed = False
            try:
                result = reschedule_with_new_session()
            except BookingNotVerified as error:
                log_message(str(error))
                send_notification("Visa booking needs manual verification", str(error))
                return 1
            except (PortalError, WebDriverException) as error:
                log_message(f"Session failed: {error}")
                session_failed = True
                result = None
            if result is not None:
                return 0
            if RUN_ONCE:
                log_message("Single session finished without a suitable slot")
                return 1 if session_failed else 0
            sleep(NEW_SESSION_DELAY)
    except KeyboardInterrupt:
        log_message("Stopped by user")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
