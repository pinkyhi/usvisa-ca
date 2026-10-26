"""Sequential visa worker with an independent durable Coordinator connection."""
import math
import os
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from usvisa.protocol.messages import GroupConfig, Message, SlotsFound
from usvisa.shared import audit
from usvisa.shared.paths import data_directory
from usvisa.transport.webstomp import WebStomp, parse_token
from usvisa.visa.appointment_client import (
    AppointmentClient, AuthenticationExpired, BookingNotVerified, PortalError, RateLimited, acceptable_dates,
)
from usvisa.visa.session_cache import SessionCache
from . import settings as cfg
from . import solo
from .bootstrap import resolve_origin
from .state import InstanceLock, NodeState


def now():
    return datetime.now(timezone.utc)


def utc_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def next_check_at(snapshot, current, earliest):
    """UTC phases with a one-second execution window for OS timer jitter.

    Local minimum spacing always wins. If a pause exceeds this window, skip the
    phase instead of catching up. The window avoids losing every second phase
    just because the previous HTTP request began milliseconds after its timer.
    """
    anchor = snapshot.start_at.timestamp()
    phase = anchor + max(0, math.floor((current - anchor) / 180)) * 180
    if current > phase + 1:
        phase += 180
    allowed = max(current, earliest)
    if allowed > phase + 1:
        phase += max(1, math.ceil((allowed - phase - 1) / 180)) * 180
    return max(phase, allowed)


class Backoff:
    def __init__(self, state, stop):
        self.state, self.stop = state, stop
        self.delay = cfg.RATE_LIMIT_BACKOFF_INITIAL_DELAY

    def reset(self):
        self.delay = cfg.RATE_LIMIT_BACKOFF_INITIAL_DELAY

    def wait(self, error):
        enabled = all(value > 0 for value in (cfg.RATE_LIMIT_BACKOFF_INITIAL_DELAY,
                       cfg.RATE_LIMIT_BACKOFF_MAX_DELAY, cfg.RATE_LIMIT_BACKOFF_MULTIPLIER))
        if enabled and self.delay > cfg.RATE_LIMIT_BACKOFF_MAX_DELAY:
            raise solo.RateLimitExhausted("Portal is still rate limited after all configured cooldowns")
        delay = max(error.retry_after, self.delay if enabled else 180)
        if enabled:
            maximum = cfg.RATE_LIMIT_BACKOFF_MAX_DELAY
            self.delay = maximum + 1 if self.delay == maximum else min(maximum, self.delay * cfg.RATE_LIMIT_BACKOFF_MULTIPLIER)
        until = max(self.state.get("cooldown", 0), time.time() + delay)
        self.state.set("cooldown", until)
        solo.log_message(f"Rate limited; next worker operation in at least {delay:g} seconds")
        self.stop.wait(max(0, until - time.time()))


@contextmanager
def authenticated_client():
    cache = SessionCache(cfg.SESSION_CACHE_DIR, cfg.USER_EMAIL) if cfg.PERSIST_SESSION else None
    client = cache.load(cfg.CONSULATES[cfg.USER_CONSULATE], cfg.USER_CONSULATE, cfg.HTTP_TIMEOUT) if cache else None
    valid = True
    if client:
        try:
            client.check_session()
            solo.log_message(f"Saved visa session verified for {cfg.USER_EMAIL}")
        except AuthenticationExpired:
            client.session.close()
            cache.invalidate()
            client = None
        except BaseException:
            client.session.close()
            raise
    if client is None:
        solo.log_message("Signing in to the visa site with Chromium")
        driver = solo.get_chrome_driver()
        try:
            for attempt in range(cfg.NEW_SESSION_AFTER_FAILURES):
                try:
                    solo.login(driver)
                    break
                except solo.WebDriverException as error:
                    if attempt + 1 == cfg.NEW_SESSION_AFTER_FAILURES:
                        raise
                    solo.log_message(f"Visa login attempt {attempt + 1} failed ({type(error).__name__}); retrying in {cfg.FAIL_RETRY_DELAY}s")
                    time.sleep(cfg.FAIL_RETRY_DELAY)
            client = AppointmentClient.from_driver(driver, cfg.CONSULATES[cfg.USER_CONSULATE],
                                                    cfg.USER_CONSULATE, cfg.HTTP_TIMEOUT)
        finally:
            solo.close_driver(driver)
        try:
            client.prepare_session()
            solo.log_message(f"Visa login successful for {cfg.USER_EMAIL}; appointment session prepared")
        except BaseException:
            client.session.close()
            raise
    try:
        solo.log_message("Visa worker ready")
        yield client
    except AuthenticationExpired:
        valid = False
        if cache:
            cache.invalidate()
        raise
    finally:
        try:
            if cache and valid:
                cache.save(client)
        finally:
            client.session.close()


class Worker:
    def __init__(self, state, transport):
        self.state, self.transport = state, transport
        self.stop = transport.stopping
        self.backoff = Backoff(state, self.stop)
        self.minimum_monotonic = 0
        self.verified_booking = self.state.get("verified_booking")

    def notify(self, result):
        try:
            if result.dry_run:
                solo.send_notification(f"[TEST] Visa slot found for {result.date}",
                    f"Would book {result.date} at {result.time}, {cfg.USER_CONSULATE}. No booking POST was sent.")
            else:
                solo.send_notification(f"Visa Appointment Rescheduled for {result.date}",
                    f"Verified appointment: {result.date} at {result.time}, {cfg.USER_CONSULATE}.")
        except Exception as error:
            solo.log_message(f"Notification failed ({type(error).__name__}); finding remains in local outbox")

    def report(self, dates, observed, exclude):
        snapshot = self.state.snapshot()
        if not snapshot:
            return
        useful = tuple(sorted({day for day in dates if day < snapshot.data.report_before and day not in exclude}))
        expires = observed + timedelta(seconds=180)
        if useful and expires > now():
            message = Message.create(SlotsFound(self.state.node_id, observed, useful), sent_at=now())
            self.state.add_finding(message, expires)

    def try_dates(self, client, dates, *, observed=None, expires=None):
        excluded, result = set(), None
        if self.verified_booking:
            excluded.add(date.fromisoformat(self.verified_booking["date"]))
        candidates = acceptable_dates(dates, date.fromisoformat(cfg.EARLIEST_ACCEPTABLE_DATE),
                                     date.fromisoformat(cfg.LATEST_ACCEPTABLE_DATE),
                                     [(date.fromisoformat(start), date.fromisoformat(end))
                                      for start, end in cfg.EXCLUSION_DATE_RANGES])
        try:
            if self.verified_booking:
                return None
            for day in candidates:
                if self.stop.is_set():
                    break
                if expires is not None and now() >= expires:
                    break
                try:
                    result = client.book(day, dry_run=cfg.TEST_MODE, **({"expires_at": expires} if expires else {}))
                except BookingNotVerified:
                    excluded.add(day)
                    raise
                if result:
                    if not result.dry_run:
                        excluded.add(result.date)
                        # Commit before reporting/email: a restart or notification
                        # failure must not re-enable automatic booking.
                        verified = {"date": result.date.isoformat(), "time": result.time}
                        self.state.set("verified_booking", verified)
                        self.verified_booking = verified
                        solo.log_booking_success(result)
                        solo.log_message("Coordinated monitoring continues; further automatic booking is disabled")
                    break
        finally:
            # Persist before notification/exit, including dates outside our own
            # filters. Received tasks are never relayed back to the Coordinator.
            if observed is not None:
                self.report(dates, observed, excluded)
        if result:
            if result.dry_run:
                solo.log_message(f"TEST: would book {result.date} at {result.time}; no booking POST was sent")
            self.notify(result)
        return result

    def poll(self, client):
        started = time.time()
        self.minimum_monotonic = time.monotonic() + 180
        self.state.set("last_check", started)
        count = self.state.get("cycle_checks", 0) + 1
        self.state.set("cycle_checks", count)
        solo.log_message(f"Checking available dates in {cfg.USER_CONSULATE}")
        try:
            dates = solo.get_available_dates(client)
            if dates is not None:
                solo.log_message(f"Check completed: {len(dates)} available date(s)")
                solo.log_message(f"Earliest: {min(dates).isoformat()}" if dates else "Portal returned empty array")
            else:
                solo.log_message("Date check failed; will retry at the next scheduled time")
            observed = now()
            result = self.try_dates(client, dates, observed=observed) if dates else None
            if dates is not None:
                self.backoff.reset()
            return result
        finally:
            if cfg.DATE_REQUEST_CYCLE_LENGTH and count >= cfg.DATE_REQUEST_CYCLE_LENGTH:
                self.state.set("cycle_checks", 0)
                self.state.set("cooldown", max(self.state.get("cooldown", 0), time.time() + cfg.DATE_REQUEST_CYCLE_GAP))

    def loop(self, client):
        due, revision = None, None
        announced_cooldown = None
        while not self.stop.is_set():
            current = time.time()
            cooldown = self.state.get("cooldown", 0)
            if current < cooldown:
                if cooldown != announced_cooldown:
                    solo.log_message(f"Worker cooldown until {utc_time(cooldown)}")
                    announced_cooldown = cooldown
                self.stop.wait(min(.5, cooldown - current))
                due = None
                continue
            task = self.state.next_task(now())
            if task:
                try:
                    self.try_dates(client, task.data.dates, expires=task.data.expires_at)
                except RateLimited as error:
                    self.state.retry_task(task.data.finding_id)
                    self.backoff.wait(error)
                except AuthenticationExpired:
                    self.state.retry_task(task.data.finding_id)
                    raise
                except BookingNotVerified:
                    self.state.finish_task(task.data.finding_id)
                    raise
                except PortalError as error:
                    solo.log_message(f"Could not prepare shared slot ({type(error).__name__})")
                    self.state.finish_task(task.data.finding_id)
                else:
                    self.state.finish_task(task.data.finding_id)
                due = None
                continue
            snapshot = self.state.snapshot()
            if not snapshot:
                self.stop.wait(.2)
                continue
            if due is None or snapshot.data.revision != revision:
                earliest = max(self.state.get("last_check", 0) + 180,
                               current + max(0, self.minimum_monotonic - time.monotonic()), cooldown)
                due = next_check_at(snapshot.data, current, earliest)
                revision = snapshot.data.revision
                solo.log_message(f"Next date check at {utc_time(due)} (schedule revision {revision})")
            if current < due:
                self.stop.wait(min(.2, due - current))
                continue
            if current > due + 1:
                due = None
                continue
            try:
                self.poll(client)
            except RateLimited as error:
                self.backoff.wait(error)
                due = None
                continue
            due = None
        if self.transport.fatal:
            raise ValueError(self.transport.fatal)

    def run(self):
        if self.verified_booking:
            solo.log_message(f"Automatic booking remains disabled after verified appointment "
                             f"{self.verified_booking['date']} at {self.verified_booking['time']}; monitoring continues")
        while not self.stop.is_set():
            if self.state.snapshot() is None:
                self.stop.wait(.2)
                continue
            try:
                with authenticated_client() as client:
                    return self.loop(client)
            except AuthenticationExpired:
                solo.log_message("Session expired; signing in again")
            except RateLimited as error:
                self.backoff.wait(error)
            except BookingNotVerified:
                raise
            except (PortalError, solo.WebDriverException) as error:
                solo.log_message(f"Session failed ({type(error).__name__})")
                self.state.set("cooldown", max(self.state.get("cooldown", 0), time.time() + cfg.FAIL_RETRY_DELAY))
                self.stop.wait(cfg.FAIL_RETRY_DELAY)
        if self.transport.fatal:
            raise ValueError(self.transport.fatal)


def main(*, check_only=False):
    try:
        cfg.validate_settings()
        token = os.environ.get("NODE_TOKEN", "")
        node_id, _ = parse_token(token)
        min_date = date.fromisoformat(os.environ.get("NODE_MIN_DATE", ""))
    except ValueError as error:
        solo.log_message(f"Invalid coordinated configuration: {error}")
        return 2
    group = GroupConfig(cfg.CONSULATES[cfg.USER_CONSULATE], cfg.USER_CONSULATE)
    directory = data_directory() / ".node-state" / str(node_id)
    try:
        lock = InstanceLock(directory / "instance.lock")
    except RuntimeError as error:
        solo.log_message(str(error))
        return 2
    state = transport = None
    code = 0
    try:
        state = NodeState(directory / "state.sqlite3", node_id, group)
        transport = WebStomp(os.environ.get("SERVER_URL", ""), token, min_date, state,
                            allow_local=os.getenv("ALLOW_INSECURE_LOCALHOST", "false").lower() == "true",
                            **({"origin_provider": resolve_origin}
                               if os.getenv("EXPOSE_MODE") == "cloudflare_quick" and os.getenv("QUICK_TUNNEL_METRICS") else {}))
        audit.event("run_started", mode="coordinated", node_id=node_id, test_mode=cfg.TEST_MODE)
        solo.log_message(f"Node starting: mode=coordinated, nodeId={node_id}, city={group.city}, "
                         f"minDate={min_date}, testMode={cfg.TEST_MODE}")
        saved = state.snapshot()
        if saved:
            solo.log_message(f"Using saved schedule: revision={saved.data.revision}, "
                             f"startAt={saved.data.start_at.isoformat()}, interval={saved.data.interval_seconds}s")
        else:
            solo.log_message("Waiting for the first schedule from Coordinator")
        transport.start()
        if check_only:
            deadline = time.monotonic() + 120
            while not transport.stopping.is_set() and time.monotonic() < deadline:
                if transport.connected.is_set() and state.snapshot():
                    solo.log_message("Coordinator connection verified; schedule received")
                    break
                transport.stopping.wait(.2)
            else:
                raise ValueError("Coordinator did not deliver a schedule")
            if transport.fatal:
                raise ValueError(transport.fatal)
        else:
            Worker(state, transport).run()
    except KeyboardInterrupt:
        solo.log_message("Stopped by user")
    except Exception as error:
        audit.event("run_failed", error_type=type(error).__name__)
        solo.log_message(str(error) if isinstance(error, (BookingNotVerified, ValueError)) else
                         f"Coordinated worker stopped ({type(error).__name__})")
        code = 1
        if isinstance(error, (BookingNotVerified, solo.RateLimitExhausted)):
            try:
                solo.send_notification("Visa booking needs manual verification" if isinstance(error, BookingNotVerified)
                                       else "Visa rescheduler stopped: rate limit", str(error))
            except Exception:
                solo.log_message("Stop notification could not be delivered")
    finally:
        if transport:
            # A finite flush; unconfirmed findings remain durable for next run.
            deadline = time.monotonic() + 5
            while code == 0 and state and state.pending(now()) and transport.connected.is_set() and time.monotonic() < deadline:
                time.sleep(.1)
            transport.stop()
        if state:
            state.close()
        lock.close()
    audit.event("run_finished", mode="coordinated", exit_code=code)
    return code
