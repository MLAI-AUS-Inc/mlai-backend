from playwright.sync_api import sync_playwright


def fetch_rendered_html(url: str, wait_ms: int = 3000) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(wait_ms)
        html = page.content()
        browser.close()
        return html