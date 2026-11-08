import logging
import time
from urllib.parse import urlsplit

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait

from dtos import V1RequestBase


def _probe(driver, probe_url):
    return driver.execute_async_script("""
        const address = arguments[0];
        const complete = arguments[arguments.length - 1];
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 4000);
        fetch(address, {credentials: 'include', cache: 'no-store', signal: controller.signal})
            .then(async response => {
                const data = await response.json();
                const errors = data.errors || [];
                complete({ok: (response.ok || response.status === 404)
                    && errors.every(error => error.code !== 'Warden::Challenge')});
            })
            .catch(() => complete({ok: false}))
            .finally(() => clearTimeout(timer));
    """, probe_url).get("ok", False)


def _dismiss_consent(driver):
    for label in ("Reject all", "Advanced Settings"):
        for button in driver.find_elements(By.XPATH, "//button[normalize-space(.)='%s']" % label):
            if button.is_displayed():
                button.click()
                return True
    return False


def _nodes(node):
    yield node
    for child in node.get("children", []) + node.get("shadowRoots", []):
        yield from _nodes(child)
    if node.get("contentDocument"):
        yield from _nodes(node["contentDocument"])


def _click_widget(driver):
    driver.execute_cdp_cmd("DOM.enable", {})
    document = driver.execute_cdp_cmd("DOM.getDocument", {"depth": -1, "pierce": True})
    for node in _nodes(document["root"]):
        if node.get("nodeName") != "IFRAME":
            continue
        attributes = node.get("attributes", [])
        source = dict(zip(attributes[::2], attributes[1::2])).get("src", "")
        if urlsplit(source).hostname != "challenges.cloudflare.com":
            continue
        driver.execute_cdp_cmd("DOM.scrollIntoViewIfNeeded", {"nodeId": node["nodeId"]})
        box = driver.execute_cdp_cmd("DOM.getBoxModel", {"nodeId": node["nodeId"]})["model"]
        if box["width"] < 200 or not 60 <= box["height"] <= 90:
            continue
        points = box["content"]
        position = {"x": points[0] + 20, "y": points[1] + 32}
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", **position})
        for event in ("mousePressed", "mouseReleased"):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": event, "button": "left", "clickCount": 1, **position,
            })
        return True
    return False


def _recover(driver, options, deadline):
    clicked = False
    next_probe = 0
    next_click = 0

    def ready(browser):
        nonlocal clicked, next_probe, next_click
        now = time.monotonic()
        try:
            inputs = browser.find_elements(By.CSS_SELECTOR, "input[name='cf-turnstile-response']")
            pending = bool(inputs) and not inputs[0].get_attribute("value")
            if now >= next_probe and (not pending or not clicked):
                next_probe = now + 5
                if _probe(browser, options["probeUrl"]):
                    return True
            if options.get("dismissConsent", False) and _dismiss_consent(browser):
                return False
            if pending and now >= next_click:
                if _click_widget(browser):
                    clicked = True
                    next_click = now + 10
                    logging.info("Embedded Turnstile checkbox activated")
        except WebDriverException:
            pass
        return False

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Embedded Turnstile recovery timed out")
    WebDriverWait(driver, remaining, poll_frequency=0.5).until(
        ready, "Embedded Turnstile recovery did not restore API access",
    )
    return clicked


def resolve(request, driver, method, resolve_page):
    options = request.embeddedTurnstile
    if method != "GET" or not isinstance(options, dict):
        raise ValueError("embeddedTurnstile must be an object on a GET request")
    address = urlsplit(options.get("probeUrl", ""))
    if address.scheme not in ("http", "https") or not address.netloc:
        raise ValueError("embeddedTurnstile.probeUrl must be an HTTP URL")
    if request.tabs_till_verify is not None:
        raise ValueError("embeddedTurnstile cannot be combined with tabs_till_verify")
    deadline = time.monotonic() + int(request.maxTimeout) / 1000
    seed = V1RequestBase({"url": options["probeUrl"], "maxTimeout": request.maxTimeout})
    resolve_page(seed, driver, method)
    result = resolve_page(request, driver, method)
    if _recover(driver, options, deadline):
        result.message = "Embedded Turnstile solved!"
    result.result.url = driver.current_url
    result.result.response = None if request.returnOnlyCookies else driver.page_source
    result.result.cookies = driver.get_cookies()
    if request.returnScreenshot:
        result.result.screenshot = driver.get_screenshot_as_base64()
    return result