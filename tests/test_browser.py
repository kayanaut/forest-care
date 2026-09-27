"""Drive the web UI in headless Chromium: review an item, open a stand, check provenance.

External map tiles are blocked so the test does not depend on the network.
Run only this file with:  uv run pytest -m browser
"""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright  # noqa: E402

pytestmark = pytest.mark.browser


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


def test_import_photo_folder_and_label_in_the_ui(page, server_url, tmp_path):
    from simulator.photos import SamplePhoto, jpeg_bytes
    paths = []
    for i, (lat, lon) in enumerate([(50.71250, 7.08976), (50.71250, 7.09040)]):  # Venusberg woodland, 45 m apart
        path = tmp_path / f"IMG_UI{i + 1}.JPG"
        path.write_bytes(jpeg_bytes(SamplePhoto(path.name, "Prunus serotina", lat, lon, synthetic_marker=False), seed=i))
        paths.append(str(path))

    page.goto(f"{server_url}/#missions")
    page.locator("summary", has_text="Import field photos").click()
    page.set_input_files("#photo-files", paths)
    page.fill("#photo-by", "UI Photographer")
    page.fill("#photo-area", "UI import test")
    page.click("#photo-import")
    expect(page.locator(".import-report")).to_contain_text("2 photo(s) imported")
    expect(page.locator(".import-report")).to_contain_text("field photo")

    page.locator(".import-report a", has_text="review them").click()
    page.locator(".card.clickable", has_text="IMG_UI1.JPG").click()
    expect(page.locator(".panel-body")).to_contain_text("Taken by a person")
    expect(page.locator("#original-link")).to_be_visible()
    obs_id = page.url.rsplit("/", 1)[-1]

    page.fill("#reviewer", "UI Test Expert")
    page.fill("#label-count", "4")
    page.select_option("#label-phen", "fruiting")
    page.locator("button[data-decision='confirmed']").click()
    page.wait_for_function(f"location.hash !== '#obs/{obs_id}'")  # the next queue item opened

    detail = page.request.get(f"{server_url}/api/observations/{obs_id}").json()
    last = detail["reviews"][-1]
    assert (last["decision"], last["plant_count"], last["phenology"], last["reviewer"]) == ("confirmed", 4, "fruiting", "UI Test Expert")
    assert detail["source_kind"] == "field_photos"
    original = page.request.get(f"{server_url}{detail['original_url']}")
    assert original.body() == open(paths[0], "rb").read()
    assert page.errors == []
