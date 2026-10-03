"""Offline tests with synthetic portal HTML; no accounts or network are used."""

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import MagicMock, Mock, patch

import requests
from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException

import notifications
import reschedule
import settings
from appointment_client import (
    AppointmentClient, AuthenticationExpired, BookingNotVerified, BookingResult,
    PortalError, acceptable_dates, appointment_matches, form_fields,
)
from request_tracker import RequestTracker


URL = "https://ais.usvisa-info.com/en-ca/niv/schedule/123/appointment"
DAY = date(2026, 11, 12)
FORM = f"""
<form id="appointment-form" method="post" action="{URL}">
  <input name="utf8" value="✓">
  <input type="hidden" name="authenticity_token" value="fresh-token">
  <input name="confirmed_limit_message" value="0">
  <input name="appointments[applicant_ids][]" value="101">
  <input name="appointments[applicant_ids][]" value="102">
  <select name="appointments[consulate_appointment][facility_id]">
    <option value="94" selected>Toronto</option><option value="95">Vancouver</option>
  </select>
  <input name="appointments[consulate_appointment][date]" value="">
  <select name="appointments[consulate_appointment][time]"><option value="">Select</option></select>
  <input type="checkbox" name="unchecked" value="1">
  <input type="checkbox" name="checked" value="1" checked>
  <input name="disabled" value="secret" disabled>
  <input type="submit" name="commit" value="Reschedule">
</form>
"""
SAVED = """
<table>
  <tr><th>Consular Section Interview Date:</th><td>12 November, 2026, 08:30</td></tr>
  <tr><th>Consular Section Location:</th><td>U.S. Consulate Vancouver</td></tr>
</table>
"""
OLD = SAVED.replace("12 November", "20 December")


def response(html="", payload=None, url=URL, status=200):
    result = requests.Response()
    result.url = url
    result.status_code = status
    result._content = (json.dumps(payload) if payload is not None else html).encode("utf-8")
    result.encoding = "utf-8"
    return result


class NoNetworkTest(unittest.TestCase):
    def setUp(self):
        blocker = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected live HTTP"))
        blocker.start()
        self.addCleanup(blocker.stop)


class BookingTests(NoNetworkTest):
    def setUp(self):
        super().setUp()
        self.session = Mock(spec=requests.Session)
        self.client = AppointmentClient(self.session, URL, 95, "Vancouver", timeout=17)

    def prepare(self, saved=SAVED, form=FORM, times=None):
        if times is None:
            times = ["10:00", "08:30"]
        self.session.get.side_effect = [
            response(form), response(payload={"available_times": times}),
            response(saved, url=URL + "/instructions"), response("<html></html>"),
        ]
        self.session.post.return_value = response("Successfully Scheduled")

    def test_dry_run_queries_form_and_times_without_booking_post(self):
        self.prepare()
        result = self.client.book(DAY, dry_run=True)
        self.assertEqual(result, BookingResult(DAY, "08:30", True))
        self.session.post.assert_not_called()
        self.assertEqual(self.session.get.call_count, 2)
        self.assertEqual(self.session.get.call_args.kwargs["params"]["date"], DAY.isoformat())

    def test_booking_preserves_form_fields_and_verifies_fresh_saved_record(self):
        self.prepare()
        result = self.client.book(DAY, dry_run=False)
        self.assertFalse(result.dry_run)
        self.session.post.assert_called_once()
        args = self.session.post.call_args
        self.assertEqual(args.args, (URL,))
        fields = args.kwargs["data"]
        values = dict(fields)
        self.assertEqual(values["authenticity_token"], "fresh-token")
        self.assertEqual(values["confirmed_limit_message"], "1")
        self.assertEqual(values["appointments[consulate_appointment][facility_id]"], "95")
        self.assertEqual(values["appointments[consulate_appointment][date]"], DAY.isoformat())
        self.assertEqual(values["appointments[consulate_appointment][time]"], "08:30")
        self.assertEqual([value for name, value in fields if name.endswith("[applicant_ids][]")],
                         ["101", "102"])
        self.assertNotIn("unchecked", values)
        self.assertNotIn("disabled", values)
        self.assertEqual(values["commit"], "Reschedule")
        self.assertEqual(args.kwargs["headers"]["X-CSRF-Token"], "fresh-token")
        self.assertNotIn("X-Requested-With", args.kwargs["headers"])
        self.assertTrue(all(call.kwargs["timeout"] == 17 for call in self.session.get.call_args_list))
        self.assertEqual(self.session.get.call_args.args, (URL + "/instructions",))

    def test_success_text_is_not_proof_of_booking(self):
        self.prepare(saved=OLD)
        with self.assertRaises(BookingNotVerified):
            self.client.book(DAY, dry_run=False)
        self.session.post.assert_called_once()

    def test_form_echo_and_unlabelled_date_cannot_confirm_booking(self):
        echoed = FORM.replace('value="">', f'value="{DAY.isoformat()}">')
        self.prepare(saved=echoed + "<p>Vancouver 2026-11-12 08:30 Successfully Scheduled</p>")
        with self.assertRaises(BookingNotVerified):
            self.client.book(DAY, dry_run=False)

    def test_post_timeout_with_saved_record_is_success_without_second_post(self):
        self.prepare()
        self.session.post.side_effect = requests.Timeout()
        self.assertFalse(self.client.book(DAY, dry_run=False).dry_run)
        self.session.post.assert_called_once()

    def test_post_timeout_without_saved_record_stops_without_resubmission(self):
        self.prepare(saved=OLD)
        self.session.post.side_effect = requests.Timeout()
        with self.assertRaisesRegex(BookingNotVerified, "no automatic booking retry"):
            self.client.book(DAY, dry_run=False)
        self.session.post.assert_called_once()

    def test_empty_times_never_submit(self):
        self.prepare(times=[])
        self.assertIsNone(self.client.book(DAY, dry_run=False))
        self.session.post.assert_not_called()

    def test_invalid_or_missing_csrf_blocks_submission(self):
        self.prepare(form=FORM.replace('value="fresh-token"', 'value=""'))
        with self.assertRaisesRegex(PortalError, "CSRF"):
            self.client.book(DAY, dry_run=False)
        self.session.post.assert_not_called()

    def test_meta_csrf_fallback(self):
        self.prepare(form='<meta name="csrf-token" content="meta-token">' +
                     FORM.replace('value="fresh-token"', 'value=""'))
        self.client.book(DAY, dry_run=False)
        self.assertEqual(dict(self.session.post.call_args.kwargs["data"])["authenticity_token"], "meta-token")

    def test_unexpected_form_action_cannot_post_to_wrong_route(self):
        self.prepare(form=FORM.replace(f'action="{URL}"', 'action="/cancel"'))
        with self.assertRaisesRegex(PortalError, "action"):
            self.client.book(DAY, dry_run=False)
        self.session.post.assert_not_called()

    def test_required_asc_fields_are_not_silently_submitted_empty(self):
        self.prepare(form=FORM.replace("</form>", '<input required name="appointments[asc_appointment][date]"></form>'))
        with self.assertRaisesRegex(PortalError, "ASC"):
            self.client.book(DAY, dry_run=False)
        self.session.post.assert_not_called()

    def test_login_redirect_requires_new_selenium_session(self):
        self.session.get.return_value = response("<html></html>", url="https://ais.usvisa-info.com/en-ca/niv/users/sign_in")
        with self.assertRaises(AuthenticationExpired):
            self.client.get_available_dates()

    def test_html_login_form_requires_new_session_even_with_200(self):
        self.session.get.return_value = response('<input type="email" id="user_email">')
        with self.assertRaises(AuthenticationExpired):
            self.client.get_available_dates()

    def test_malformed_json_is_reported_without_response_body(self):
        self.session.get.return_value = response("private-account-data")
        with self.assertRaises(PortalError) as caught:
            self.client.get_available_dates()
        self.assertNotIn("private-account-data", str(caught.exception))

    def test_available_dates_are_sorted_and_empty_array_is_valid(self):
        self.session.get.return_value = response(payload=[{"date": "2026-12-20"}, {"date": DAY.isoformat()}])
        self.assertEqual(self.client.get_available_dates(), [DAY, date(2026, 12, 20)])
        self.session.get.return_value = response(html="[]")
        self.assertEqual(self.client.get_available_dates(), [])

    def test_unexpected_availability_schema_and_invalid_times_are_rejected(self):
        for payload in ({}, ["bad-date"], [{"date": "invalid"}]):
            with self.subTest(payload=payload):
                self.session.get.return_value = response(payload=payload)
                with self.assertRaises(PortalError):
                    self.client.get_available_dates()
        for payload in ([], {"available_times": ["25:00"]}, {"available_times": [None]}):
            with self.subTest(payload=payload):
                self.session.get.return_value = response(payload=payload)
                with self.assertRaises(PortalError):
                    self.client.get_available_times(DAY)

    def test_wrong_location_is_rejected_even_if_footer_mentions_requested_city(self):
        wrong = SAVED.replace("Consulate Vancouver", "Consulate Toronto") + "<footer>Vancouver</footer>"
        self.assertFalse(self.client._saved_appointment_matches(wrong, DAY, "08:30"))

    def test_wrong_time_and_date_are_rejected(self):
        self.assertFalse(self.client._saved_appointment_matches(SAVED, DAY, "10:00"))
        self.assertFalse(self.client._saved_appointment_matches(OLD, DAY, "08:30"))

    def test_conflicting_consular_location_cannot_be_overridden_by_summary(self):
        html = SAVED.replace("Consulate Vancouver", "Consulate Toronto")
        html += '<p class="consular-appt">12 November, 2026, 08:30 Vancouver</p>'
        self.assertFalse(self.client._saved_appointment_matches(html, DAY, "08:30"))

    def test_account_page_verification_is_scoped_to_correct_application(self):
        other = '<div class="application"><a href="/en-ca/niv/schedule/999/appointment">Continue</a>' \
                '<p class="consular-appt">12 November, 2026, 08:30 Vancouver</p></div>'
        own = '<div class="application"><a href="/en-ca/niv/schedule/123/appointment">Continue</a>' \
              '<p class="consular-appt">20 December, 2026, 08:30 Vancouver</p></div>'
        self.assertFalse(self.client._saved_appointment_matches(other + own, DAY, "08:30", True))
        self.assertTrue(self.client._saved_appointment_matches(
            other + own.replace("20 December", "12 November"), DAY, "08:30", True))

    def test_instructions_redirect_does_not_verify_other_application(self):
        self.session.get.side_effect = [
            response('<p class="consular-appt">12 November, 2026, 08:30 Vancouver</p>',
                     url="https://ais.usvisa-info.com/en-ca/niv/users/999"),
            response("<html></html>"),
        ]
        self.assertFalse(self.client.verify_booking(DAY, "08:30"))

    def test_selenium_cookies_keep_domain_path_expiry_and_user_agent(self):
        driver = Mock()
        driver.current_url = URL
        driver.execute_script.return_value = "Browser User Agent"
        driver.get_cookies.return_value = [
            {"name": "_yatri_session", "value": "test-session", "domain": "ais.usvisa-info.com",
             "path": "/", "secure": True, "expiry": 2147483647},
        ]
        with AppointmentClient.from_driver(driver, 95, "Vancouver") as client:
            cookie = next(iter(client.session.cookies))
            self.assertEqual(cookie.domain, "ais.usvisa-info.com")
            self.assertTrue(cookie.secure)
            self.assertEqual(cookie.expires, 2147483647)
            prepared = client.session.prepare_request(requests.Request("GET", URL))
            self.assertIn("_yatri_session=test-session", prepared.headers["Cookie"])
            self.assertEqual(prepared.headers["User-Agent"], "Browser User Agent")

    def test_exclusions_are_inclusive_and_later_suitable_dates_are_found(self):
        dates = [date(2026, 12, 20), date(2026, 11, 13), DAY, date(2026, 11, 1)]
        self.assertEqual(acceptable_dates(dates, DAY, date(2026, 11, 30), [(DAY, DAY)]),
                         [date(2026, 11, 13)])

    def test_date_time_matching_supports_english_formats_and_am_pm(self):
        for text in ("12 November, 2026, 08:30", "November 12, 2026 8:30 AM", "2026-11-12 08:30"):
            self.assertTrue(appointment_matches(text, DAY, "08:30"))
        self.assertTrue(appointment_matches("12 November, 2026, 12:00 AM", DAY, "00:00"))
        self.assertFalse(appointment_matches("12 November, 2026 8:30 PM", DAY, "08:30"))


class RunnerTests(NoNetworkTest):
    def setUp(self):
        super().setUp()
        configuration = {
            "EARLIEST_ACCEPTABLE_DATE": "2026-11-12", "LATEST_ACCEPTABLE_DATE": "2026-12-20",
            "EXCLUSION_DATE_RANGES": [], "USER_CONSULATE": "Vancouver", "TEST_MODE": True,
            "DATE_REQUEST_DELAY": 0,
            "RUN_ONCE": True, "FAIL_RETRY_DELAY": 0,
            "NEW_SESSION_AFTER_FAILURES": 1,
            "PERSIST_SESSION": False,
        }
        patcher = patch.multiple(reschedule, **configuration)
        patcher.start()
        self.addCleanup(patcher.stop)
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_run_once_checks_dates_once_without_waiting(self):
        for dates in ([], [date(2027, 1, 1)]):
            with self.subTest(dates=dates):
                client = Mock()
                client.get_available_dates.return_value = dates
                with patch.object(reschedule, "sleep") as sleep:
                    self.assertIsNone(reschedule.reschedule(client))
                client.get_available_dates.assert_called_once()
                client.book.assert_not_called()
                sleep.assert_not_called()

    def test_continuous_polling_uses_same_client_every_240_seconds(self):
        client = Mock()
        client.get_available_dates.side_effect = [[]] * 7 + [[DAY]]
        client.book.return_value = BookingResult(DAY, "08:30", True)
        with patch.multiple(reschedule, RUN_ONCE=False, DATE_REQUEST_DELAY=240), \
             patch.object(reschedule, "sleep") as sleep, \
             patch.object(reschedule, "send_notification"):
            self.assertIsNotNone(reschedule.reschedule(client))
        self.assertEqual(client.get_available_dates.call_count, 8)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [240] * 7)

    def test_polling_skips_date_without_times_and_does_not_claim_dry_run_success(self):
        client = Mock()
        client.get_available_dates.return_value = [DAY, date(2026, 11, 13)]
        client.book.side_effect = [None, BookingResult(date(2026, 11, 13), "08:30", True)]
        with patch.object(reschedule, "send_notification", return_value=False) as notify:
            result = reschedule.reschedule(client)
        self.assertTrue(result.dry_run)
        self.assertTrue(notify.call_args.args[0].startswith("[TEST]"))
        self.assertEqual(client.book.call_count, 2)

    def test_gmail_failure_does_not_retry_verified_booking(self):
        client = Mock()
        client.get_available_dates.return_value = [DAY]
        client.book.return_value = BookingResult(DAY, "08:30", False)
        with patch.object(reschedule, "TEST_MODE", False), \
             patch.object(reschedule, "send_notification", return_value=False):
            self.assertFalse(reschedule.reschedule(client).dry_run)
        client.book.assert_called_once_with(DAY, dry_run=False)

    def test_browser_is_closed_before_http_polling_and_http_client_is_closed(self):
        driver = Mock()
        context = MagicMock()
        client = context.__enter__.return_value
        result = BookingResult(DAY, "08:30", True)

        def poll(current_client):
            driver.quit.assert_called_once()
            self.assertIs(current_client, client)
            return result

        with patch.object(reschedule, "get_chrome_driver", return_value=driver), \
             patch.object(reschedule, "login"), patch.object(reschedule, "get_appointment_page"), \
             patch.object(reschedule, "_prepare_appointment_page"), \
             patch.object(reschedule.AppointmentClient, "from_driver", return_value=context), \
             patch.object(reschedule, "reschedule", side_effect=poll):
            self.assertEqual(reschedule.reschedule_with_new_session(), result)
        context.__exit__.assert_called_once()

    def test_browser_is_closed_after_login_failure(self):
        driver = Mock()
        with patch.object(reschedule, "get_chrome_driver", return_value=driver), \
             patch.object(reschedule, "login", side_effect=TimeoutException()):
            with self.assertRaises(PortalError):
                reschedule.reschedule_with_new_session()
        driver.quit.assert_called_once()

    def test_unverified_post_stops_main_without_new_session(self):
        with patch.object(reschedule, "validate_settings"), \
             patch.object(reschedule, "reschedule_with_new_session",
                          side_effect=BookingNotVerified("Check account")) as attempt, \
             patch.object(reschedule, "send_notification"):
            self.assertEqual(reschedule.main(), 1)
        attempt.assert_called_once()


class NotificationTests(NoNetworkTest):
    def test_unconfigured_gmail_does_not_connect(self):
        with patch.multiple(settings, GMAIL_EMAIL="", GMAIL_APPLICATION_PWD="", RECEIVER_EMAIL=""), \
             patch.object(notifications.smtplib, "SMTP") as smtp:
            self.assertFalse(notifications.send_notification("subject", "body"))
        smtp.assert_not_called()

    def test_gmail_uses_starttls_app_password_and_handles_delivery_failure(self):
        with patch.multiple(settings, GMAIL_EMAIL="sender@example.com",
                            GMAIL_APPLICATION_PWD="abcd efgh ijkl mnop", RECEIVER_EMAIL="receiver@example.com"), \
             patch.object(notifications.smtplib, "SMTP") as smtp:
            connection = smtp.return_value.__enter__.return_value
            self.assertTrue(notifications.send_notification("subject", "body"))
            connection.starttls.assert_called_once()
            connection.login.assert_called_once_with("sender@example.com", "abcdefghijklmnop")
            connection.send_message.side_effect = notifications.smtplib.SMTPException("offline")
            with self.assertLogs("notifications", level="WARNING"):
                self.assertFalse(notifications.send_notification("subject", "body"))


class TrackerAndSettingsTests(NoNetworkTest):
    def test_retry_limit_allows_exactly_configured_number_of_requests(self):
        with redirect_stdout(io.StringIO()):
            tracker = RequestTracker(2, 900)
            self.assertTrue(tracker.should_retry())
            tracker.retry()
            self.assertTrue(tracker.should_retry())
            tracker.retry()
            self.assertFalse(tracker.should_retry())

    def test_retry_time_limit_uses_monotonic_clock(self):
        with patch("request_tracker.time.monotonic", side_effect=[100, 110]), redirect_stdout(io.StringIO()):
            self.assertFalse(RequestTracker(2, 10).should_retry())

    def test_test_mode_boolean_default_is_safe_and_explicit_false_works(self):
        with patch.dict(settings.os.environ, {}, clear=True):
            self.assertTrue(settings.env_bool("TEST_MODE", True))
        with patch.dict(settings.os.environ, {"TEST_MODE": "false"}):
            self.assertFalse(settings.env_bool("TEST_MODE", True))

    def test_settings_validate_missing_credentials_and_single_day_exclusions(self):
        configuration = {
            "USER_EMAIL": "test@example.com", "USER_PASSWORD": "dummy-test-password",
            "USER_CONSULATE": "Vancouver", "EARLIEST_ACCEPTABLE_DATE": DAY.isoformat(),
            "LATEST_ACCEPTABLE_DATE": "2026-12-20", "EXCLUSION_DATE_RANGES": [(DAY.isoformat(), DAY.isoformat())],
        }
        with patch.multiple(settings, **configuration):
            settings.validate_settings()
            with patch.object(settings, "USER_PASSWORD", ""):
                with self.assertRaisesRegex(ValueError, "USER_PASSWORD"):
                    settings.validate_settings()


if __name__ == "__main__":
    unittest.main()
