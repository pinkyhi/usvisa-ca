import unittest
from unittest.mock import patch

from usvisa.visa import login


class BrowserTests(unittest.TestCase):
    def test_headless_identity_matches_the_normal_installed_chromium(self):
        with patch.object(login, "SHOW_GUI", False), patch.object(login.webdriver, "Chrome") as chrome:
            driver = chrome.return_value
            driver.execute_script.return_value = "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/156.0.0.0 Safari/537.36"
            self.assertIs(login.get_chrome_driver(), driver)
            driver.execute_cdp_cmd.assert_called_once_with("Network.setUserAgentOverride", {
                "userAgent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/156.0.0.0 Safari/537.36"})

    def test_visible_browser_keeps_its_existing_identity(self):
        with patch.object(login, "SHOW_GUI", True), patch.object(login.webdriver, "Chrome") as chrome:
            login.get_chrome_driver()
            chrome.return_value.execute_cdp_cmd.assert_not_called()

    def test_browser_is_closed_if_initial_configuration_fails(self):
        with patch.object(login, "SHOW_GUI", False), patch.object(login.webdriver, "Chrome") as chrome:
            chrome.return_value.execute_script.side_effect = RuntimeError("Browser failed")
            with self.assertRaises(RuntimeError):
                login.get_chrome_driver()
            chrome.return_value.quit.assert_called_once()
