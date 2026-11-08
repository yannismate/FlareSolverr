import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.webdriver import WebDriver

import embedded_turnstile as extension
import flaresolverr_service
from dtos import V1RequestBase


class ImmediateWait:
    def __init__(self, driver, timeout, **kwargs):
        self.driver = driver

    def until(self, predicate, message):
        for attempt in range(5):
            if predicate(self.driver):
                return True
        raise TimeoutException(message)


class EmbeddedTurnstileTests(unittest.TestCase):
    def setUp(self):
        self.original = Mock()
        self.driver = Mock(spec=WebDriver)
        self.driver.current_url = "https://example.com/profile"
        self.driver.page_source = "recovered page"
        self.token_input = Mock()
        self.token_input.get_attribute.return_value = ""
        self.driver.get_cookies.return_value = [{"name": "clearance", "value": "not logged"}]
        self.result = SimpleNamespace(result=SimpleNamespace())
        self.original.return_value = self.result
        self.service_patch = patch.object(flaresolverr_service, "_resolve_page", self.original)
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.resolve = flaresolverr_service._evil_logic

    def request(self, **kwargs):
        return V1RequestBase({"url": "https://example.com/profile", "maxTimeout": 10000, **kwargs})

    def test_default_behavior_is_unchanged(self):
        request = self.request()
        with patch.object(extension, "resolve") as resolve:
            self.assertIs(self.resolve(request, self.driver, "GET"), self.result)
        resolve.assert_not_called()
        self.original.assert_called_once_with(request, self.driver, "GET")
        self.driver.execute_cdp_cmd.assert_not_called()

    def test_invalid_options(self):
        for options in (True, {}, {"probeUrl": "file:///etc/passwd"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.resolve(self.request(embeddedTurnstile=options), self.driver, "GET")
        self.original.assert_not_called()

    def test_recovery_is_get_only(self):
        with self.assertRaises(ValueError):
            self.resolve(self.request(embeddedTurnstile={"probeUrl": "https://api.example.com/profile"}), self.driver, "POST")
        self.original.assert_not_called()

    def test_tab_solver_cannot_be_combined_with_recovery(self):
        with self.assertRaises(ValueError):
            self.resolve(self.request(embeddedTurnstile={"probeUrl": "https://api.example.com/profile"}, tabs_till_verify=1), self.driver, "GET")
        self.original.assert_not_called()

    def test_recovery_refreshes_response_and_keeps_driver(self):
        request = self.request(embeddedTurnstile={"probeUrl": "https://api.example.com/profile"}, returnScreenshot=True)
        with patch.object(extension, "_recover") as recover:
            self.resolve(request, self.driver, "GET")
        self.assertEqual(self.original.call_count, 2)
        self.assertEqual(self.original.call_args_list[0].args[0].url, request.embeddedTurnstile["probeUrl"])
        self.assertIs(self.original.call_args_list[0].args[1], self.driver)
        recover.assert_called_once()
        self.assertEqual(self.result.result.response, "recovered page")
        self.assertEqual(self.result.result.cookies, self.driver.get_cookies.return_value)
        self.driver.get_screenshot_as_base64.assert_called_once()

    def test_recovery_logs_stages_with_session(self):
        request = self.request(session="session-id", embeddedTurnstile={"probeUrl": "https://api.example.com/profile"})
        with patch.object(extension, "_recover", return_value=False), self.assertLogs(level="INFO") as logs:
            self.resolve(request, self.driver, "GET")
        for stage in ("api_clearance", "application_navigation", "verification", "complete"):
            self.assertTrue(any("session=session-id stage=" + stage in entry for entry in logs.output))

    def test_delayed_widget_and_removed_input(self):
        self.driver.find_elements.side_effect = [[], [self.token_input], [], [], []]
        with patch.object(extension, "WebDriverWait", ImmediateWait), \
                patch.object(extension.time, "monotonic", side_effect=[0, 1, 2, 3, 4, 6]), \
                patch.object(extension, "_probe", side_effect=[False, True]), \
                patch.object(extension, "_click_widget", return_value=True) as click:
            extension._recover(self.driver, {"probeUrl": "https://api.example.com/profile"}, 100)
        click.assert_called_once_with(self.driver)

    def test_unresolved_challenge_times_out(self):
        self.driver.find_elements.return_value = [self.token_input]
        with patch.object(extension, "WebDriverWait", ImmediateWait), \
                patch.object(extension.time, "monotonic", side_effect=range(20)), \
                patch.object(extension, "_probe", return_value=False) as probe, \
                patch.object(extension, "_click_widget", return_value=True) as click:
            with self.assertRaises(TimeoutException):
                extension._recover(self.driver, {"probeUrl": "https://api.example.com/profile"}, 100)
        click.assert_called_once()
        probe.assert_called_once()

    def test_incomplete_checkbox_is_retried_without_api_polling(self):
        self.driver.find_elements.side_effect = [[self.token_input]] * 4 + [[]]
        with patch.object(extension, "WebDriverWait", ImmediateWait), \
                patch.object(extension.time, "monotonic", side_effect=[0, 1, 6, 11, 16, 21]), \
                patch.object(extension, "_probe", side_effect=[False, True]) as probe, \
                patch.object(extension, "_click_widget", return_value=True) as click:
            self.assertTrue(extension._recover(self.driver, {"probeUrl": "https://api.example.com/profile"}, 100))
        self.assertEqual(click.call_count, 2)
        self.assertEqual(probe.call_count, 2)

    def test_consent_is_dismissed_before_clicking_widget(self):
        self.driver.find_elements.side_effect = [[self.token_input]] * 3 + [[], []]
        with patch.object(extension, "WebDriverWait", ImmediateWait), \
                patch.object(extension.time, "monotonic", side_effect=[0, 1, 2, 3, 4, 6]), \
                patch.object(extension, "_probe", side_effect=[False, True]), \
                patch.object(extension, "_dismiss_consent", side_effect=[True, True, False, False]) as consent, \
                patch.object(extension, "_click_widget", return_value=True) as click:
            self.assertTrue(extension._recover(self.driver, {
                "probeUrl": "https://api.example.com/profile", "dismissConsent": True,
            }, 100))
        self.assertEqual(consent.call_count, 4)
        click.assert_called_once()

    def test_widget_in_closed_shadow_root_uses_relative_coordinates(self):
        self.driver.execute_cdp_cmd.side_effect = [
            {}, {"root": {"shadowRoots": [{"children": [{"nodeName": "IFRAME", "nodeId": 7,
                "attributes": ["src", "https://challenges.cloudflare.com/widget"]}]}]}},
            {},
            {"model": {"width": 300, "height": 65, "content": [400, 200, 700, 200, 700, 265, 400, 265]}},
            {}, {}, {},
        ]
        self.assertTrue(extension._click_widget(self.driver))
        self.driver.execute_cdp_cmd.assert_any_call("Input.dispatchMouseEvent", {
            "type": "mousePressed", "button": "left", "clickCount": 1, "x": 420, "y": 232,
        })


if __name__ == "__main__":
    unittest.main()