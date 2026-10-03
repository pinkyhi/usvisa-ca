"""Offline checks for portal throttling and retry pacing."""

import unittest
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, patch

import requests
import reschedule
from appointment_client import AppointmentClient, AuthenticationExpired, RateLimited


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.multiple(reschedule, DATE_REQUEST_CYCLE_LENGTH=0,
            RATE_LIMIT_BACKOFF_INITIAL_DELAY=4, RATE_LIMIT_BACKOFF_MAX_DELAY=4096,
            RATE_LIMIT_BACKOFF_MULTIPLIER=2, EMPTY_DATES_BACKOFF_INITIAL_DELAY=240,
            EMPTY_DATES_BACKOFF_MAX_DELAY=3840, EMPTY_DATES_BACKOFF_MULTIPLIER=2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_429_preserves_session_and_parses_retry_after(self):
        session = Mock()
        client = AppointmentClient(session,
            "https://ais.usvisa-info.com/en-ca/niv/schedule/123/appointment",
            95, "Vancouver")
        response = requests.Response()
        response.status_code = 429
        response.url = client.appointment_url
        response.headers["Retry-After"] = "600"
        session.get.return_value = response
        with self.assertRaises(RateLimited) as caught:
            client.get_available_dates()
        self.assertEqual(caught.exception.retry_after, 600)
        session.close.assert_not_called()

    def test_rate_limit_during_verification_stops_further_requests(self):
        session = Mock()
        client = AppointmentClient(session,
            "https://ais.usvisa-info.com/en-ca/niv/schedule/123/appointment",
            95, "Vancouver")
        with patch.object(client, "_get", side_effect=RateLimited("600")) as get:
            self.assertFalse(client.verify_booking(date(2026, 11, 12), "08:30"))
        get.assert_called_once()

    def test_retry_after_date_and_invalid_values(self):
        until = datetime.now(timezone.utc) + timedelta(seconds=600)
        delay = RateLimited(format_datetime(until, usegmt=True)).retry_after
        self.assertGreater(delay, 598)
        self.assertLessEqual(delay, 600)
        for value in (None, "garbage", "-10"):
            self.assertEqual(RateLimited(value).retry_after, 0)

    def test_polling_doubles_and_resets_after_success(self):
        client = Mock()
        client.get_available_dates.side_effect = [
            RateLimited(), RateLimited(), RateLimited("1000"), [], RateLimited(), [], KeyboardInterrupt(),
        ]
        with patch.multiple(reschedule, DATE_REQUEST_DELAY=180,
                RUN_ONCE=False, EARLIEST_ACCEPTABLE_DATE="2026-11-01",
                LATEST_ACCEPTABLE_DATE="2026-12-01", EXCLUSION_DATE_RANGES=[]), \
             patch.object(reschedule, "sleep") as sleep, \
             patch.object(reschedule, "log_message"):
            with self.assertRaises(KeyboardInterrupt):
                reschedule.reschedule(client)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         [4, 8, 1000, 240, 4, 480])
        client.book.assert_not_called()

    def test_time_endpoint_throttling_also_doubles_without_booking_retry(self):
        client = Mock()
        client.get_available_dates.side_effect = [[date(2026, 11, 12)]] * 3 + [KeyboardInterrupt()]
        client.book.side_effect = [RateLimited(), RateLimited(), None]
        with patch.multiple(reschedule, DATE_REQUEST_DELAY=180,
                RUN_ONCE=False,
                EARLIEST_ACCEPTABLE_DATE="2026-11-01",
                LATEST_ACCEPTABLE_DATE="2026-12-01", EXCLUSION_DATE_RANGES=[]), \
             patch.object(reschedule, "sleep") as sleep, \
             patch.object(reschedule, "log_message"):
            with self.assertRaises(KeyboardInterrupt):
                reschedule.reschedule(client)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [4, 8, 180])

    def test_backoff_survives_session_boundaries(self):
        with patch.object(reschedule, "validate_settings"), \
             patch.object(reschedule, "RUN_ONCE", False), \
             patch.object(reschedule, "sleep"), \
             patch.object(reschedule, "log_message"), \
             patch.object(reschedule, "reschedule_with_new_session") as run:
            delays = []

            def attempt(*, backoff):
                delays.append(backoff.delay)
                backoff.wait(RateLimited())
                return None if len(delays) == 1 else object()

            run.side_effect = attempt
            self.assertEqual(reschedule.main(), 0)
        self.assertEqual(delays, [4, 8])

    def test_exhausted_backoff_notifies_and_stops(self):
        client = Mock()
        client.get_available_dates.side_effect = RateLimited()
        with patch.multiple(reschedule, EARLIEST_ACCEPTABLE_DATE="2026-11-01",
                LATEST_ACCEPTABLE_DATE="2026-12-01", EXCLUSION_DATE_RANGES=[],
                RUN_ONCE=False), \
             patch.object(reschedule, "validate_settings"), \
             patch.object(reschedule, "log_message"), \
             patch.object(reschedule, "sleep") as sleep, \
             patch.object(reschedule, "send_notification", return_value=True) as notify, \
             patch.object(reschedule, "reschedule_with_new_session") as run:
            run.side_effect = lambda *, backoff: reschedule.reschedule(client, backoff=backoff)
            self.assertEqual(reschedule.main(), 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         [4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096])
        self.assertEqual(client.get_available_dates.call_count, 12)
        notify.assert_called_once()
        client.book.assert_not_called()

    def test_expired_session_restarts_only_when_run_once_is_false(self):
        for run_once in (True, False):
            with self.subTest(run_once=run_once), \
                 patch.object(reschedule, "RUN_ONCE", run_once), \
                 patch.object(reschedule, "validate_settings"), \
                 patch.object(reschedule, "log_message"), \
                 patch.object(reschedule, "sleep") as sleep, \
                 patch.object(reschedule, "reschedule_with_new_session",
                              side_effect=[AuthenticationExpired("HTTP 401"), object()]) as run:
                self.assertEqual(reschedule.main(), 1 if run_once else 0)
                self.assertEqual(run.call_count, 1 if run_once else 2)
                sleep.assert_not_called()

    def test_run_once_does_not_retry_or_wait_on_429(self):
        client = Mock()
        client.get_available_dates.side_effect = RateLimited("600")
        with patch.multiple(reschedule, RUN_ONCE=True,
                EARLIEST_ACCEPTABLE_DATE="2026-11-01",
                LATEST_ACCEPTABLE_DATE="2026-12-01", EXCLUSION_DATE_RANGES=[]), \
             patch.object(reschedule, "validate_settings"), \
             patch.object(reschedule, "log_message"), \
             patch.object(reschedule, "sleep") as sleep, \
             patch.object(reschedule, "reschedule_with_new_session") as run:
            run.side_effect = lambda *, backoff: reschedule.reschedule(client, backoff=backoff)
            self.assertEqual(reschedule.main(), 1)
        run.assert_called_once()
        client.get_available_dates.assert_called_once()
        sleep.assert_not_called()

    def test_empty_dates_cap_persists_after_nonempty_reply(self):
        client = Mock()
        client.get_available_dates.side_effect = ([[]] * 6
            + [[date(2027, 1, 1)], [], KeyboardInterrupt()])
        with patch.multiple(reschedule, RUN_ONCE=False, DATE_REQUEST_DELAY=180,
                EARLIEST_ACCEPTABLE_DATE="2026-11-01", LATEST_ACCEPTABLE_DATE="2026-12-01",
                EXCLUSION_DATE_RANGES=[]), patch.object(reschedule, "sleep") as sleep, \
                patch.object(reschedule, "log_message"):
            with self.assertRaises(KeyboardInterrupt):
                reschedule.reschedule(client)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         [240, 480, 960, 1920, 3840, 3840, 180, 3840])
        client.book.assert_not_called()

    def test_empty_delay_survives_reauthentication(self):
        delays = []
        def attempt(*, backoff):
            delays.append(backoff.empty_delay)
            if len(delays) == 1:
                backoff.wait_empty()
                raise AuthenticationExpired("HTTP 401")
            return object()
        with patch.object(reschedule, "RUN_ONCE", False), \
             patch.object(reschedule, "validate_settings"), \
             patch.object(reschedule, "log_message"), \
             patch.object(reschedule, "sleep") as sleep, \
             patch.object(reschedule, "reschedule_with_new_session", side_effect=attempt):
            self.assertEqual(reschedule.main(), 0)
        self.assertEqual(delays, [240, 480])
        sleep.assert_called_once_with(240)

    def test_custom_429_backoff_reaches_cap_then_stops(self):
        with patch.multiple(reschedule, RATE_LIMIT_BACKOFF_INITIAL_DELAY=3,
                RATE_LIMIT_BACKOFF_MAX_DELAY=10, RATE_LIMIT_BACKOFF_MULTIPLIER=3), \
             patch.object(reschedule, "sleep") as sleep, patch.object(reschedule, "log_message"):
            backoff = reschedule.RateLimitBackoff()
            for _ in range(3):
                backoff.wait(RateLimited())
            with self.assertRaises(reschedule.RateLimitExhausted):
                backoff.wait(RateLimited())
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3, 9, 10])

    def test_empty_backoff_and_cycle_gap_use_longer_pause(self):
        client = Mock()
        client.get_available_dates.side_effect = [[], [], [], KeyboardInterrupt()]
        with patch.multiple(reschedule, RUN_ONCE=False, DATE_REQUEST_CYCLE_LENGTH=1,
                DATE_REQUEST_CYCLE_GAP=900, EARLIEST_ACCEPTABLE_DATE="2026-11-01",
                LATEST_ACCEPTABLE_DATE="2026-12-01", EXCLUSION_DATE_RANGES=[]), \
             patch.object(reschedule, "sleep") as sleep, patch.object(reschedule, "log_message"):
            with self.assertRaises(KeyboardInterrupt):
                reschedule.reschedule(client)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [900, 900, 960])

    def test_zero_parameter_disables_only_its_backoff(self):
        for prefix in ("RATE_LIMIT_BACKOFF", "EMPTY_DATES_BACKOFF"):
            for suffix in ("INITIAL_DELAY", "MAX_DELAY", "MULTIPLIER"):
                with self.subTest(prefix=prefix, suffix=suffix), \
                     patch.object(reschedule, f"{prefix}_{suffix}", 0), \
                     patch.object(reschedule, "sleep") as sleep, \
                     patch.object(reschedule, "log_message"):
                    backoff = reschedule.RateLimitBackoff()
                    self.assertEqual(backoff.rate_limit_enabled, prefix != "RATE_LIMIT_BACKOFF")
                    self.assertEqual(backoff.empty_enabled, prefix != "EMPTY_DATES_BACKOFF")
                    for _ in range(15):
                        if prefix == "RATE_LIMIT_BACKOFF":
                            backoff.wait(RateLimited())
                        else:
                            backoff.wait_empty()
                    sleep.assert_not_called()
                    if prefix == "RATE_LIMIT_BACKOFF":
                        backoff.wait(RateLimited("600"), minimum_delay=180)
                        sleep.assert_called_once_with(600)
                    else:
                        backoff.wait_empty(minimum_delay=180)
                        sleep.assert_called_once_with(180)

    def test_disabled_backoffs_use_polling_delay_and_cycle_gap(self):
        client = Mock()
        client.get_available_dates.side_effect = [
            RateLimited(), [], RateLimited("600"), [], KeyboardInterrupt(),
        ]
        with patch.multiple(reschedule, RUN_ONCE=False, DATE_REQUEST_DELAY=180,
                DATE_REQUEST_CYCLE_LENGTH=2, DATE_REQUEST_CYCLE_GAP=900,
                RATE_LIMIT_BACKOFF_INITIAL_DELAY=0, EMPTY_DATES_BACKOFF_INITIAL_DELAY=0,
                EARLIEST_ACCEPTABLE_DATE="2026-11-01", LATEST_ACCEPTABLE_DATE="2026-12-01",
                EXCLUSION_DATE_RANGES=[]), patch.object(reschedule, "sleep") as sleep, \
                patch.object(reschedule, "log_message"):
            with self.assertRaises(KeyboardInterrupt):
                reschedule.reschedule(client)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [180, 900, 600, 900])
