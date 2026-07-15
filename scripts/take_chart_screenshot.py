#!/usr/bin/env python3
import asyncio
import argparse
from playwright.async_api import async_playwright

async def take_screenshot(url: str, output_file: str, selector: str = None, full_page: bool = True):
    """
    Navigates to a URL and takes a screenshot of the page or a specific element.

    Args:
        url (str): The URL to navigate to.
        output_file (str): The path to save the screenshot file.
        selector (str, optional): The CSS selector of the element to screenshot. 
                                  If None, screenshots the full page. Defaults to None.
        full_page (bool): Whether to take a screenshot of the full scrollable page.
                          Used when selector is None. Defaults to True.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        try:
            print(f"Navigating to {url}...")
            await page.goto(url, wait_until="networkidle")
            print("Page loaded.")

            if selector:
                print(f"Looking for element with selector: {selector}")
                element = page.locator(selector)
                await element.wait_for()
                print("Element found. Taking screenshot...")
                await element.screenshot(path=output_file)
            else:
                print("Taking full page screenshot...")
                await page.screenshot(path=output_file, full_page=full_page)

            print(f"Screenshot saved to {output_file}")

        except Exception as e:
            print(f"An error occurred: {e}")
        finally:
            await browser.close()
            print("Browser closed.")

async def main():
    parser = argparse.ArgumentParser(
        description="Take a screenshot of a trading chart from a URL.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "url", 
        help="The URL of the trading chart page."
    )
    parser.add_argument(
        "-o", "--output", 
        required=True, 
        dest="output_file",
        help="Path to save the output screenshot file (e.g., chart.png)."
    )
    parser.add_argument(
        "-s", "--selector", 
        default=None,
        help="CSS selector of a specific chart element to capture.\nIf omitted, a full page screenshot will be taken."
    )
    parser.add_argument(
        "--no-full-page",
        action="store_false",
        dest="full_page",
        help="Capture only the visible viewport instead of the full scrollable page.\nOnly applies when no selector is provided."
    )

    args = parser.parse_args()

    await take_screenshot(args.url, args.output_file, args.selector, args.full_page)

if __name__ == "__main__":
    asyncio.run(main())
