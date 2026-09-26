"""Drive the web UI in headless Chromium: review an item, open a stand, check provenance.

External map tiles are blocked so the test does not depend on the network.
Run only this file with:  uv run pytest -m browser
"""

import socket
import threading
import time

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright  # noqa: E402

from forestcare.api import create_app  # noqa: E402

pytestmark = pytest.mark.browser


@pytest.fixture
def server_url(seeded_settings):
    import uvicorn
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(seeded_settings), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def page(server_url):
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not available: {exc}")
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(server_url) else route.abort())
        page.errors = errors
        yield page
        browser.close()


def test_review_an_observation_in_the_ui(page, server_url):
    page.goto(f"{server_url}/#review")
    expect(page.locator("#sim-banner")).to_be_visible()
    count = page.locator("#count-review")
    expect(count).not_to_have_text("")
    before = int(count.inner_text())
    assert before > 0

    page.locator(".panel-body .card.clickable").first.click()
    expect(page.locator(".decision-buttons")).to_be_visible()
    first_uid = page.locator(".panel-body h2").inner_text()

    # A decision without a reviewer name is refused.
    page.locator("button[data-decision='confirmed']").click()
    expect(page.locator("#review-error")).to_contain_text("Reviewer")

    page.fill("#reviewer", "UI Test Expert")
    page.locator("button[data-decision='confirmed']").click()
    expect(count).to_have_text(str(before - 1))
    expect(page.locator(".panel-body h2")).not_to_have_text(first_uid)  # next item opened

    # Keyboard shortcut: R rejects.
    page.locator(".panel-body h2").click()
    page.keyboard.press("r")
    expect(count).to_have_text(str(before - 2))
    assert page.errors == []


def test_stand_view_and_provenance(page, server_url):
    page.goto(f"{server_url}/#stands")
    rows = page.locator("tr.clickable")
    expect(rows.first).to_be_visible()
    page.locator(".filters button", has_text="Expanding").click()
    expect(rows).to_have_count(1)
    rows.first.click()
    expect(page.locator(".panel-body")).to_contain_text("Survey history")
    expect(page.locator(".panel-body")).to_contain_text("not a recommendation")
    expect(page.locator("svg.chart")).to_be_visible()
    expect(page.locator(".leaflet-marker-icon .stand-marker.selected")).to_have_count(1)

    page.goto(f"{server_url}/#sources")
    expect(page.locator(".panel-body")).to_contain_text("What is real and what is simulated")
    expect(page.locator(".panel-body")).to_contain_text("Datenlizenz Deutschland")
    assert page.errors == []
