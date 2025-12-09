import re
from playwright.sync_api import Page, expect

def test_homepage_has_title(page: Page):
    """
    Verifies the Playwright website loads and has the expected title.
    This serves as a quick check that Chromium/network is working.
    """
    page.goto("https://playwright.dev/")
    expect(page).to_have_title(re.compile("Playwright", re.IGNORECASE))


def test_duckduckgo_search_works(page: Page):
    """
    Verifies interaction by performing a search on DuckDuckGo using a robust locator.
    We are now using a flexible Python 're' object for the title assertion to 
    prevent the previous failure due to minor page title variations.
    """
    # NOTE: I removed the page.route optimization block for this simpler test, 
    # as it was not directly involved in the fix.
    
    page.goto("https://duckduckgo.com")
    
    query = "headless playwright python"

    # 1. Use a highly robust, composite locator for the search input.
    search_input = page.locator(
        'input[name="q"][type="text"], input[id="search_form_input_homepage"], [role="searchbox"]'
    )

    # Wait for visibility
    expect(search_input).to_be_visible(timeout=20000)

    # Type and submit the search query
    search_input.fill(query)
    search_input.press("Enter")

    # Wait for the results to load (networkidle is often reliable)
    page.wait_for_load_state("networkidle")

    # 2. ASSERTION FIX: Assert the title contains the query string and the site name.
    # We use re.compile to create a flexible regex object.
    # The pattern asserts that ANYTHING (.*) appears, followed by the escaped query, 
    # followed by ANYTHING (.*), then "DuckDuckGo". This guarantees a match.
    expected_pattern = re.compile(f".*{re.escape(query)}.*DuckDuckGo", re.IGNORECASE)
    
    expect(page).to_have_title(expected_pattern)
