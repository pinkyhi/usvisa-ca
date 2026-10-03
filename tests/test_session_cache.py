import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests

import reschedule
from appointment_client import (
    AppointmentClient, AuthenticationExpired, BookingNotVerified, BookingResult, PortalError,
)
from session_cache import SessionCache
from test_booking import DAY, NoNetworkTest, URL


class SessionCacheTests(NoNetworkTest):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.cache = SessionCache(self.directory, "test@example.com")
        session = requests.Session()
        self.addCleanup(session.close)
        session.headers["User-Agent"] = "Browser User Agent"
        session.cookies.set("_yatri_session", "initial-cookie", domain="ais.usvisa-info.com",
                            path="/", secure=True, expires=2147483647)
        session.cookies.set("same-name", "root", domain=".ais.usvisa-info.com", path="/")
        session.cookies.set("same-name", "scoped", domain=".ais.usvisa-info.com", path="/en-ca")
        self.client = AppointmentClient(session, URL, 95, "Vancouver")

    def test_round_trip_preserves_cookie_scope_expiry_user_agent_and_url(self):
        self.assertTrue(self.cache.save(self.client))
        with self.cache.load(94, "Toronto", 17) as restored:
            self.assertEqual(restored.appointment_url, URL)
            self.assertEqual(restored.facility_id, 94)
            self.assertEqual(restored.consulate, "Toronto")
            self.assertEqual(restored.timeout, 17)
            cookies = {(cookie.name, cookie.domain, cookie.path): cookie
                       for cookie in restored.session.cookies}
            auth = cookies[("_yatri_session", "ais.usvisa-info.com", "/")]
            self.assertTrue(auth.secure)
            self.assertEqual(auth.expires, 2147483647)
            self.assertEqual(cookies[("same-name", ".ais.usvisa-info.com", "/")].value, "root")
            self.assertEqual(cookies[("same-name", ".ais.usvisa-info.com", "/en-ca")].value, "scoped")
            prepared = restored.session.prepare_request(requests.Request("GET", URL))
            self.assertIn("_yatri_session=initial-cookie", prepared.headers["Cookie"])
            self.assertEqual(prepared.headers["User-Agent"], "Browser User Agent")

    def test_http_cookie_rotation_is_saved_for_next_run(self):
        self.cache.save(self.client)
        self.client.session.cookies.set("_yatri_session", "rotated-cookie", domain="ais.usvisa-info.com",
                                        path="/", secure=True, expires=2147483647)
        self.cache.save(self.client)
        with self.cache.load(95, "Vancouver") as restored:
            self.assertEqual(restored.session.cookies.get("_yatri_session"), "rotated-cookie")

    def test_file_contains_no_email_password_or_csrf_token(self):
        self.cache.save(self.client)
        payload = json.loads(self.cache.path.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"version", "appointment_url", "user_agent", "cookies"})
        self.assertNotIn("test@example.com", self.cache.path.read_text(encoding="utf-8"))
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_accounts_use_separate_files_and_email_case_is_normalized(self):
        self.cache.save(self.client)
        other = SessionCache(self.directory, "other@example.com")
        self.assertNotEqual(self.cache.path, other.path)
        self.assertIsNone(other.load(95, "Vancouver"))
        self.assertEqual(self.cache.path, SessionCache(self.directory, " TEST@example.com ").path)

    def test_missing_cache_just_requests_normal_login(self):
        self.assertIsNone(self.cache.load(95, "Vancouver"))

    def test_expired_cookies_cannot_be_restored(self):
        self.cache.save(self.client)
        payload = json.loads(self.cache.path.read_text(encoding="utf-8"))
        for cookie in payload["cookies"]:
            cookie["expires"] = 1
        self.cache.path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(self.cache.load(95, "Vancouver"))

    def test_corrupt_file_falls_back_without_logging_its_contents(self):
        self.cache.path.write_text("corrupt-private-cookie-value", encoding="utf-8")
        with self.assertLogs("session_cache", level="WARNING") as logs:
            self.assertIsNone(self.cache.load(95, "Vancouver"))
        self.assertNotIn("corrupt-private-cookie-value", " ".join(logs.output))

    def test_wrong_url_and_unknown_schema_cannot_be_restored(self):
        self.cache.save(self.client)
        original = json.loads(self.cache.path.read_text(encoding="utf-8"))
        for changes in (
            {"appointment_url": "https://example.com/en-ca/niv/schedule/123/appointment"},
            {"appointment_url": URL + "?different=schedule"},
            {"version": 999},
            {"cookies": [{"name": "malformed"}]},
        ):
            with self.subTest(changes=changes), self.assertLogs("session_cache", level="WARNING"):
                self.cache.path.write_text(json.dumps({**original, **changes}), encoding="utf-8")
                self.assertIsNone(self.cache.load(95, "Vancouver"))

    def test_failed_atomic_write_keeps_previous_file_and_removes_temporary_file(self):
        self.cache.save(self.client)
        before = self.cache.path.read_bytes()
        with patch("session_cache.os.replace", side_effect=OSError("locked")), \
             self.assertLogs("session_cache", level="WARNING"):
            self.assertFalse(self.cache.save(self.client))
        self.assertEqual(before, self.cache.path.read_bytes())
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_invalidate_only_removes_current_accounts_file(self):
        self.cache.save(self.client)
        other = SessionCache(self.directory, "other@example.com")
        other.save(self.client)
        self.cache.invalidate()
        self.assertFalse(self.cache.path.exists())
        self.assertTrue(other.path.exists())


class SessionReuseTests(NoNetworkTest):
    def setUp(self):
        super().setUp()
        configuration = {
            "PERSIST_SESSION": True, "USER_EMAIL": "test@example.com",
            "USER_CONSULATE": "Vancouver", "FAIL_RETRY_DELAY": 0,
            "NEW_SESSION_AFTER_FAILURES": 1,
        }
        patcher = patch.multiple(reschedule, **configuration)
        patcher.start()
        self.addCleanup(patcher.stop)
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.cached_client = MagicMock()
        self.cached_client.__enter__.return_value = self.cached_client
        self.cache = Mock(spec=SessionCache)
        self.cache.load.return_value = self.cached_client
        self.result = BookingResult(DAY, "08:30", True)

    def test_valid_saved_session_skips_chrome_and_refreshes_cache_on_exit(self):
        with patch.object(reschedule, "SessionCache", return_value=self.cache), \
             patch.object(reschedule, "get_chrome_driver") as browser, \
             patch.object(reschedule, "reschedule", return_value=self.result):
            self.assertEqual(reschedule.reschedule_with_new_session(), self.result)
        browser.assert_not_called()
        self.cached_client.check_session.assert_called_once()
        self.cached_client.__exit__.assert_called_once()
        self.cache.save.assert_called_once_with(self.cached_client)
        self.cache.invalidate.assert_not_called()

    def test_valid_session_without_slots_does_not_trigger_another_login(self):
        with patch.object(reschedule, "SessionCache", return_value=self.cache), \
             patch.object(reschedule, "get_chrome_driver") as browser, \
             patch.object(reschedule, "reschedule", return_value=None):
            self.assertIsNone(reschedule.reschedule_with_new_session())
        browser.assert_not_called()

    def test_expired_session_is_invalidated_and_selenium_login_is_used(self):
        self.cached_client.check_session.side_effect = AuthenticationExpired("expired")
        driver = Mock()
        context = MagicMock()
        fresh_client = context.__enter__.return_value
        with patch.object(reschedule, "SessionCache", return_value=self.cache), \
             patch.object(reschedule, "get_chrome_driver", return_value=driver) as browser, \
             patch.object(reschedule, "login") as login, \
             patch.object(reschedule, "get_appointment_page"), \
             patch.object(reschedule, "_prepare_appointment_page"), \
             patch.object(reschedule.AppointmentClient, "from_driver", return_value=context), \
             patch.object(reschedule, "reschedule", return_value=self.result):
            self.assertEqual(reschedule.reschedule_with_new_session(), self.result)
        self.cache.invalidate.assert_called_once()
        self.cache.save.assert_called_once_with(fresh_client)
        browser.assert_called_once()
        login.assert_called_once_with(driver)
        driver.quit.assert_called_once()

    def test_expiry_during_http_polling_also_falls_back_to_selenium(self):
        driver = Mock()
        context = MagicMock()
        with patch.object(reschedule, "SessionCache", return_value=self.cache), \
             patch.object(reschedule, "get_chrome_driver", return_value=driver), \
             patch.object(reschedule, "login") as login, \
             patch.object(reschedule, "get_appointment_page"), \
             patch.object(reschedule, "_prepare_appointment_page"), \
             patch.object(reschedule.AppointmentClient, "from_driver", return_value=context), \
             patch.object(reschedule, "reschedule",
                          side_effect=[AuthenticationExpired("expired"), self.result]):
            self.assertEqual(reschedule.reschedule_with_new_session(), self.result)
        login.assert_called_once()
        self.cache.invalidate.assert_called_once()
        self.cache.save.assert_called_once_with(context.__enter__.return_value)

    def test_transient_http_failure_keeps_session_and_does_not_reauthenticate(self):
        self.cached_client.check_session.side_effect = PortalError("timeout")
        with patch.object(reschedule, "SessionCache", return_value=self.cache), \
             patch.object(reschedule, "get_chrome_driver") as browser:
            with self.assertRaises(PortalError):
                reschedule.reschedule_with_new_session()
        browser.assert_not_called()
        self.cache.invalidate.assert_not_called()
        self.cache.save.assert_called_once_with(self.cached_client)

    def test_unverified_post_still_stops_without_selenium_or_booking_retry(self):
        with patch.object(reschedule, "SessionCache", return_value=self.cache), \
             patch.object(reschedule, "get_chrome_driver") as browser, \
             patch.object(reschedule, "reschedule", side_effect=BookingNotVerified("check account")) as poll:
            with self.assertRaises(BookingNotVerified):
                reschedule.reschedule_with_new_session()
        browser.assert_not_called()
        poll.assert_called_once()
        self.cache.save.assert_called_once_with(self.cached_client)

    def test_disabling_persistence_does_not_read_or_write_cache(self):
        driver = Mock()
        context = MagicMock()
        with patch.object(reschedule, "PERSIST_SESSION", False), \
             patch.object(reschedule, "SessionCache") as cache_class, \
             patch.object(reschedule, "get_chrome_driver", return_value=driver), \
             patch.object(reschedule, "login"), patch.object(reschedule, "get_appointment_page"), \
             patch.object(reschedule, "_prepare_appointment_page"), \
             patch.object(reschedule.AppointmentClient, "from_driver", return_value=context), \
             patch.object(reschedule, "reschedule", return_value=self.result):
            self.assertEqual(reschedule.reschedule_with_new_session(), self.result)
        cache_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
