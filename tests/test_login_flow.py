"""Tests for post-login Search Projects flow and page-only failure classification."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

import script_clean as sc


def _fake_driver(url, title, text, *, email=False, password=False, cookies=None):
    driver = mock.Mock()
    driver.current_url = url
    driver.title = title
    body = mock.Mock()
    body.text = text

    def find_element(by, value):
        if by == By.TAG_NAME and value == "body":
            return body
        if value == "email":
            el = mock.Mock()
            return el
        if value == "password":
            el = mock.Mock()
            return el
        if by == By.XPATH:
            el = mock.Mock()
            return el
        raise Exception(f"no element {value}")

    def find_elements(by, value):
        if value == "email":
            return [mock.Mock()] if email else []
        if value == "password":
            return [mock.Mock()] if password else []
        return []

    driver.find_element.side_effect = find_element
    driver.find_elements.side_effect = find_elements
    driver.get_cookies.return_value = cookies if cookies is not None else []
    driver.get = mock.Mock()
    return driver


class WholeWordAndPageOnlyClassifierTests(unittest.TestCase):
    def test_heap_address_2fa_in_exception_is_not_mfa(self):
        driver = _fake_driver(
            "https://app.gocatalant.com/app/next/expert/profile",
            "Consultant Profile | Catalant",
            "Finish account setup Verify email So we can always reach you",
        )
        exc = TimeoutException("Timeout @ 0x2fa deadbeef mfa two-factor")
        self.assertEqual(sc.classify_login_failure(driver, exc), "LOGGED_IN_NO_CARDS")

    def test_2fa_substring_in_page_text_is_not_mfa(self):
        driver = _fake_driver(
            "https://app.gocatalant.com/app/next/expert/profile",
            "Profile",
            "debug 0x2fa platform",
        )
        self.assertEqual(sc.classify_login_failure(driver), "LOGGED_IN_NO_CARDS")

    def test_profile_verify_email_checklist_is_not_verification_wall(self):
        driver = _fake_driver(
            "https://app.gocatalant.com/app/next/expert/profile",
            "Consultant Profile | Catalant",
            "Finish account setup 2 of 6 Verify email So we can always reach you with opportunities",
        )
        self.assertEqual(sc.classify_login_failure(driver), "LOGGED_IN_NO_CARDS")

    def test_auth_not_verified_url_is_verification_page(self):
        driver = _fake_driver(
            "https://app.gocatalant.com/auth/not-verified/",
            "Verify",
            "Please confirm your account",
        )
        self.assertEqual(sc.classify_login_failure(driver), "VERIFICATION_PAGE")

    def test_blocking_check_your_email_is_verification_page(self):
        driver = _fake_driver(
            "https://app.gocatalant.com/auth/check",
            "Check your inbox",
            "Check your email for a one-time link to continue",
        )
        self.assertEqual(sc.classify_login_failure(driver), "VERIFICATION_PAGE")

    def test_login_next_search_query_is_still_login_form(self):
        driver = _fake_driver(
            "https://app.gocatalant.com/c/_/auth/login/?next=https://app.gocatalant.com/c/_/u/0/search/?form_name=SearchForm",
            "Login | Catalant",
            "Email Password Login With Email Email Me A One-Time Link Reset Your Password",
            email=True,
            password=True,
        )
        self.assertEqual(sc.classify_login_failure(driver), "LOGIN_FORM")
        self.assertFalse(sc._has_left_login_page(driver))
        self.assertFalse(sc._is_logged_in_url(driver.current_url))

    def test_search_url_without_cards_is_logged_in_no_cards(self):
        driver = _fake_driver(
            sc.SEARCH_URL,
            "Search Projects | Catalant",
            "Filters Search Projects showing 0 projects",
        )
        self.assertEqual(sc.classify_login_failure(driver), "LOGGED_IN_NO_CARDS")

    def test_whole_word_mfa_on_page_still_classifies(self):
        driver = _fake_driver(
            "https://app.gocatalant.com/login",
            "Login",
            "Enter your 2fa code to continue",
            email=True,
            password=True,
        )
        # Login form is checked before MFA so this stays LOGIN_FORM.
        self.assertEqual(sc.classify_login_failure(driver), "LOGIN_FORM")
        driver2 = _fake_driver(
            "https://app.gocatalant.com/auth/mfa",
            "Two-factor",
            "Enter your 2fa code to continue",
        )
        self.assertEqual(sc.classify_login_failure(driver2), "MFA_REQUIRED")


class LeftLoginPageTests(unittest.TestCase):
    def test_app_next_profile_counts_as_left_login(self):
        driver = _fake_driver(
            "https://app.gocatalant.com/app/next/expert/profile",
            "Consultant Profile | Catalant",
            "Ahmed Ghazi",
        )
        self.assertTrue(sc._has_left_login_page(driver))

    def test_dashboard_with_login_form_has_not_left(self):
        driver = _fake_driver(
            sc.DASHBOARD_URL,
            "Login",
            "Login",
            email=True,
            password=True,
        )
        self.assertFalse(sc._has_left_login_page(driver))


class SaveCookiesLogTests(unittest.TestCase):
    def test_logs_saved_n_and_session_expiry(self):
        driver = _fake_driver(
            "https://app.gocatalant.com/app/next/expert/profile",
            "Profile",
            "ok",
            cookies=[
                {"name": "session", "value": "secret", "expiry": 1790000000},
                {"name": "other", "value": "x"},
            ],
        )
        buf = io.StringIO()
        with mock.patch.object(sc.db, "save_scraper_session"), mock.patch(
            "builtins.open", mock.mock_open()
        ), mock.patch.object(sc.json, "dump"), mock.patch("sys.stdout", buf):
            sc.save_cookies(driver)
        out = buf.getvalue()
        self.assertIn("Saved 2 cookies", out)
        self.assertIn("session cookie expiry:", out)
        self.assertNotIn("secret", out)
        self.assertIn("UTC", out)

    def test_cookie_for_restore_drops_null_expiry(self):
        payload = sc._cookie_for_restore(
            {"name": "ss", "value": "tok", "domain": ".app.gocatalant.com", "expiry": None}
        )
        self.assertNotIn("expiry", payload)
        self.assertEqual(payload["name"], "ss")

    def test_falls_back_to_ss_cookie_when_session_missing(self):
        self.assertEqual(
            sc._session_cookie_expiry_label([{"name": "ss"}]),
            "ss no expiry attribute",
        )


class PerformLoginFlowTests(unittest.TestCase):
    def test_success_saves_cookies_then_opens_search_not_dashboard(self):
        driver = _fake_driver(
            "https://app.gocatalant.com/app/next/expert/profile",
            "Profile",
            "ok",
            email=True,
            password=True,
        )
        got = []
        driver.get.side_effect = lambda url: got.append(url)
        save = mock.Mock()
        wait_timeouts = []

        class _Wait:
            def __init__(self, _driver, timeout):
                wait_timeouts.append(timeout)

            def until(self, _cond):
                return True

        with mock.patch.object(sc, "WebDriverWait", _Wait), mock.patch.object(
            sc, "save_cookies", save
        ), mock.patch.object(sc.time, "sleep"), mock.patch.object(
            sc, "send_error_notification", return_value=False
        ):
            result = sc.perform_login(driver)

        self.assertTrue(result["success"])
        self.assertEqual(got[0], "https://app.gocatalant.com/c/_/u/0/dashboard/")
        self.assertEqual(got[1], sc.SEARCH_URL)
        self.assertEqual(got.count(sc.DASHBOARD_URL), 1)
        save.assert_called()
        self.assertGreaterEqual(save.call_count, 2)
        self.assertEqual(wait_timeouts, [20, 30, 40])

    def test_timeout_on_cards_classifies_logged_in_no_cards(self):
        driver = _fake_driver(
            "https://app.gocatalant.com/app/next/expert/profile",
            "Consultant Profile | Catalant",
            "Finish account setup Verify email",
            email=False,
            password=False,
        )
        wait_n = {"n": 0}

        class _Wait:
            def __init__(self, _driver, _timeout):
                pass

            def until(self, _cond):
                wait_n["n"] += 1
                if wait_n["n"] >= 3:
                    raise TimeoutException("cards")
                return True

        with mock.patch.object(sc, "WebDriverWait", _Wait), mock.patch.object(
            sc, "save_cookies"
        ), mock.patch.object(sc.time, "sleep"), mock.patch.object(
            sc, "send_error_notification", return_value=False
        ) as alert, mock.patch.object(sc, "save_login_failure_evidence", return_value=[]):
            result = sc.perform_login(driver)

        self.assertFalse(result["success"])
        self.assertEqual(result["classification"], "LOGGED_IN_NO_CARDS")
        alert.assert_called()

    def test_still_on_login_form_after_submit(self):
        driver = _fake_driver(
            sc.DASHBOARD_URL,
            "Login | Catalant",
            "Login",
            email=True,
            password=True,
        )
        wait_n = {"n": 0}

        class _Wait:
            def __init__(self, _driver, _timeout):
                pass

            def until(self, _cond):
                wait_n["n"] += 1
                if wait_n["n"] >= 2:
                    raise TimeoutException("still login")
                return True

        with mock.patch.object(sc, "WebDriverWait", _Wait), mock.patch.object(
            sc.time, "sleep"
        ), mock.patch.object(sc, "send_error_notification", return_value=False), mock.patch.object(
            sc, "save_login_failure_evidence", return_value=[]
        ):
            result = sc.perform_login(driver)

        self.assertFalse(result["success"])
        self.assertEqual(result["classification"], "LOGIN_FORM")

    def test_verification_page_after_submit(self):
        driver = _fake_driver(
            "https://app.gocatalant.com/auth/not-verified/",
            "Not verified",
            "Please verify",
        )
        wait_n = {"n": 0}

        class _Wait:
            def __init__(self, _driver, _timeout):
                pass

            def until(self, _cond):
                wait_n["n"] += 1
                if wait_n["n"] >= 3:
                    raise TimeoutException("cards")
                return True

        with mock.patch.object(sc, "WebDriverWait", _Wait), mock.patch.object(
            sc, "save_cookies"
        ), mock.patch.object(sc.time, "sleep"), mock.patch.object(
            sc, "send_error_notification", return_value=False
        ), mock.patch.object(sc, "save_login_failure_evidence", return_value=[]):
            result = sc.perform_login(driver)

        self.assertFalse(result["success"])
        self.assertEqual(result["classification"], "VERIFICATION_PAGE")


class CookieLoginSavesCookiesTests(unittest.TestCase):
    def test_successful_cookie_login_saves_cookies(self):
        driver = _fake_driver(sc.SEARCH_URL, "Search Projects", "projects")
        save = mock.Mock()

        class _Wait:
            def __init__(self, _driver, _timeout):
                pass

            def until(self, _cond):
                return True

        with mock.patch.object(sc, "load_cookies", return_value=True), mock.patch.object(
            sc, "_navigate_to_search"
        ) as nav, mock.patch.object(sc, "WebDriverWait", _Wait), mock.patch.object(
            sc, "save_cookies", save
        ), mock.patch.object(sc, "perform_login") as login:
            result = sc.setup_session(driver)

        self.assertTrue(result["success"])
        self.assertEqual(result["message"], "cookies")
        nav.assert_called_once_with(driver)
        save.assert_called_once_with(driver)
        login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
