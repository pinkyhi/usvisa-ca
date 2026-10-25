"""Selenium sign-in; the HTTP client takes over after authentication."""

from selenium import webdriver
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from usvisa.node.settings import (
    DETACH, LOGIN_URL, SHOW_GUI, TIMEOUT, USER_EMAIL, USER_PASSWORD,
)


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
    WebDriverWait(driver, timeout).until(
        lambda current: "/users/sign_in" not in current.current_url,
        message="Login did not redirect to the account page",
    )
