"""Offline checks for portal throttling and retry pacing."""

import unittest
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, patch

import requests
import reschedule
from appointment_client import AppointmentClient, AuthenticationExpired, RateLimited


class RateLimitTests(unittest.TestCase):
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
            RateLimited(), RateLimited(), RateLimited("1000"), [], RateLimited(), [],
        ]
        with patch.multiple(reschedule, DATE_REQUEST_DELAY=180,
                DATE_REQUEST_MAX_RETRY=5,
                DATE_REQUEST_MAX_TIME=9000, EARLIEST_ACCEPTABLE_DATE="2026-11-01",
                LATEST_ACCEPTABLE_DATE="2026-12-01", EXCLUSION_DATE_RANGES=[]), \
             patch.object(reschedule, "sleep") as sleep, \
             patch.object(reschedule, "log_message"):
            reschedule.reschedule(client)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         [4, 8, 1000, 180, 4, 180])
        client.book.assert_not_called()

    def test_time_endpoint_throttling_also_doubles_without_booking_retry(self):
        client = Mock()
        client.get_available_dates.return_value = [date(2026, 11, 12)]
        client.book.side_effect = [RateLimited(), RateLimited(), None]
        with patch.multiple(reschedule, DATE_REQUEST_DELAY=180,
                DATE_REQUEST_MAX_RETRY=2, DATE_REQUEST_MAX_TIME=9000,
                EARLIEST_ACCEPTABLE_DATE="2026-11-01",
                LATEST_ACCEPTABLE_DATE="2026-12-01", EXCLUSION_DATE_RANGES=[]), \
             patch.object(reschedule, "sleep") as sleep, \
             patch.object(reschedule, "log_message"):
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

    def test_exhausted_backoff_notifies_and_stops_despite_polling_limits(self):
        client = Mock()
        client.get_available_dates.side_effect = RateLimited()
        with patch.multiple(reschedule, DATE_REQUEST_MAX_RETRY=1,
                DATE_REQUEST_MAX_TIME=1, EARLIEST_ACCEPTABLE_DATE="2026-11-01",
                LATEST_ACCEPTABLE_DATE="2026-12-01", EXCLUSION_DATE_RANGES=[],
                RUN_ONCE=True), \
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
