"""HTTP discovery and setup after the Selenium sign-in handoff."""

from unittest.mock import Mock

import requests

from appointment_client import AppointmentClient, PortalError, AuthenticationExpired, RateLimited
from tests.unit.test_booking import NoNetworkTest, FORM, URL, response


ACCOUNT = "https://ais.usvisa-info.com/en-ca/niv"


class HttpSetupTests(NoNetworkTest):
    def setUp(self):
        super().setUp()
        self.session = Mock(spec=requests.Session)
        self.client = AppointmentClient(self.session, None, 95, "Vancouver",
                                        account_url=ACCOUNT)

    def test_account_links_discover_one_schedule_without_any_post(self):
        self.session.get.side_effect = [
            response(f'<a href="{URL}">Continue</a>'
                     f'<a href="{ACCOUNT}/schedule/123/payment">Payment</a>', url=ACCOUNT),
            response(FORM),
        ]
        self.client.prepare_session()
        self.assertEqual(self.client.schedule_id, "123")
        self.assertEqual(self.client.appointment_url, URL)
        self.assertEqual([call.args[0] for call in self.session.get.call_args_list], [ACCOUNT, URL])
        self.session.post.assert_not_called()

    def test_group_continue_and_setup_links_use_http(self):
        group = ACCOUNT + "/groups/42"
        setup = ACCOUNT + "/schedule/123/continue"
        self.session.get.side_effect = [
            response(f'<a href="{group}">Continue</a>', url=ACCOUNT),
            response(f'<a href="{URL}">Schedule Appointment</a>', url=group),
            response(f'<a href="{setup}">Continue</a>'),
            response(FORM),
        ]
        self.client.prepare_session()
        self.assertEqual([call.args[0] for call in self.session.get.call_args_list],
                         [ACCOUNT, group, URL, setup])
        self.session.post.assert_not_called()

    def test_consent_uses_csrf_and_checked_policy_without_booking_fields(self):
        consent = f'''<form method="post" action="{ACCOUNT}/schedule/123/continue">
          <input name="authenticity_token" value="consent-token">
          <input type="hidden" name="policy" value="0">
          <input type="checkbox" name="policy" value="1">
          <input type="submit" name="commit" value="Continue">
        </form>'''
        self.session.get.side_effect = [
            response(f'<a href="{URL}">Continue</a>', url=ACCOUNT),
            response(consent), response(FORM),
        ]
        self.session.post.return_value = response("Consent accepted")
        self.client.prepare_session()
        self.session.post.assert_called_once()
        call = self.session.post.call_args
        self.assertEqual(call.args[0], ACCOUNT + "/schedule/123/continue")
        self.assertEqual(dict(call.kwargs["data"]),
                         {"authenticity_token": "consent-token", "policy": "1", "commit": "Continue"})

    def test_multiple_applications_do_not_select_arbitrarily(self):
        self.session.get.return_value = response(
            f'<a href="{URL}">Continue</a><a href="{ACCOUNT}/schedule/999/appointment">Continue</a>',
            url=ACCOUNT)
        with self.assertRaisesRegex(PortalError, "Multiple applications"):
            self.client.prepare_session()
        self.assertEqual(self.session.get.call_count, 1)
        self.session.post.assert_not_called()

    def test_get_limit_acknowledgement_needs_no_csrf_and_is_preserved(self):
        acknowledgement = f'''<form method="get" action="{URL}">
          <input type="checkbox" name="confirmed_limit_message" value="1">
          <input type="submit" name="commit" value="Continue">
        </form>'''
        self.session.get.side_effect = [
            response(f'<a href="{URL}">Continue</a>', url=ACCOUNT),
            response(acknowledgement), response(FORM), response(FORM),
        ]
        self.client.prepare_session()
        self.assertEqual(dict(self.session.get.call_args.kwargs["params"]),
                         {"commit": "Continue", "confirmed_limit_message": "1"})
        self.client.check_session()
        self.assertEqual(self.session.get.call_args.kwargs["params"],
                         {"confirmed_limit_message": "1"})
        self.session.post.assert_not_called()

    def test_saved_session_can_acknowledge_limit_with_read_only_get(self):
        self.client._set_appointment_url(URL)
        self.session.get.side_effect = [response('''<form method="get">
          <input type="checkbox" name="confirmed_limit_message" value="1">
          <input type="submit" name="commit" value="Continue"></form>'''), response(FORM)]
        self.client.check_session()
        self.session.post.assert_not_called()

    def test_get_acknowledgement_cannot_target_another_schedule(self):
        self.client._set_appointment_url(URL)
        self.session.get.return_value = response(f'''<form method="get"
            action="{ACCOUNT}/schedule/999/appointment">
          <input type="checkbox" name="confirmed_limit_message" value="1">
          <input type="submit" name="commit" value="Continue"></form>''')
        with self.assertRaises(PortalError):
            self.client.check_session()
        self.session.get.assert_called_once()
        self.session.post.assert_not_called()

    def test_external_continue_link_is_rejected_before_request(self):
        self.session.get.return_value = response(
            '<a href="https://example.com/continue">Continue</a>', url=ACCOUNT)
        with self.assertRaises(PortalError):
            self.client.prepare_session()
        self.assertEqual(self.session.get.call_count, 1)

    def test_login_expiry_and_rate_limit_are_propagated_during_discovery(self):
        for status, error in ((401, AuthenticationExpired), (429, RateLimited)):
            with self.subTest(status=status):
                self.session.get.return_value = response(status=status, url=ACCOUNT)
                with self.assertRaises(error):
                    self.client.prepare_session()

    def test_unrecognized_form_is_never_submitted(self):
        self.session.get.side_effect = [
            response(f'<a href="{URL}">Continue</a>', url=ACCOUNT),
            response('<form method="post"><input name="commit" value="Confirm"></form>'),
        ]
        with self.assertRaises(PortalError):
            self.client.prepare_session()
        self.session.post.assert_not_called()
