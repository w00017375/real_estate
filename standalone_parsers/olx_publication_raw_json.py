"""Standalone raw OLX publication inspector.

The script is intentionally independent from the estate-parser project.  It
opens one OLX publication in Playwright and writes a JSON envelope containing
the same main sections as the project (title, price, housing parameters,
description, seller, amenities and nearby places), while retaining source
names and values exactly as displayed.  It also includes the complete page
HTML, every JSON script block and every JSON network response observed while
the page loads.  Nothing is normalized, filtered or mapped to the project's
schema.

Usage:
    python -m standalone_parsers.olx_publication_raw_json "https://www.olx.uz/d/obyavlenie/..."
    python -m standalone_parsers.olx_publication_raw_json URL --output output_json/olx/publication.json

Install Playwright once if needed:
    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Вывести необработанные JSON-данные одной публикации OLX"
    )
    parser.add_argument("url", help="Полный URL публикации OLX")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Файл для JSON-результата; по умолчанию выводится в stdout",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Показать окно Chromium во время загрузки",
    )
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=2500,
        help="Сколько миллисекунд ждать после загрузки страницы (по умолчанию: 2500)",
    )
    return parser.parse_args()


def _raw_first(page: Any, selectors: list[str], attribute: str | None = None) -> str | None:
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            item = locator.nth(index)
            try:
                value = (
                    item.get_attribute(attribute)
                    if attribute
                    else item.inner_text()
                )
            except Exception:
                continue
            if value is not None:
                return value
    return None


def _raw_nodes(page: Any, selectors: list[str]) -> list[dict[str, str]]:
    """Return every matching DOM node without trimming or de-duplicating it."""

    result: list[dict[str, str]] = []
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            try:
                result.append(
                    {
                        "selector": selector,
                        "index": str(index),
                        "text": locator.nth(index).inner_text(),
                    }
                )
            except Exception:
                result.append(
                    {"selector": selector, "index": str(index), "text": ""}
                )
    return result


def _publication_sections(page: Any, requested_url: str) -> dict[str, Any]:
    """Extract source-facing sections; values are deliberately left raw."""

    phone_selectors = [
        'a[href^="tel:"]',
        '[data-testid="phone-number"]',
        '[data-testid="contact-phone"]',
        '[data-cy="ad-contact-phone"]',
    ]
    for selector in (
        'button[data-testid="show-phone"]',
        '[data-testid="show-phone"] button',
        'button[data-cy="ad-contact-phone"]',
        'button:has-text("Показать номер")',
        'button:has-text("Показать телефон")',
        'button:has-text("Raqamni ko‘rsatish")',
    ):
        try:
            button = page.locator(selector)
            if button.count():
                button.first.click(timeout=3000)
                page.wait_for_timeout(700)
                break
        except Exception:
            pass

    parameter_selectors = [
        '[data-testid="ad-parameters-container"] li',
        '[data-testid="ad-parameters-container"] p',
        '[data-testid="ad-parameters-container"] div',
        '[data-cy="ad-parameters"] li',
    ]
    nearby_selectors = [
        '[data-testid*="nearby"] li',
        '[data-testid*="nearby"] p',
        '[data-cy*="nearby"] li',
    ]
    amenities_selectors = [
        '[data-testid*="amenit"] li',
        '[data-testid*="amenit"] p',
        '[data-cy*="amenit"] li',
    ]
    images: list[dict[str, str | None]] = []
    image_locator = page.locator("img")
    for index in range(image_locator.count()):
        image = image_locator.nth(index)
        images.append(
            {
                "src": image.get_attribute("src"),
                "srcset": image.get_attribute("srcset"),
                "alt": image.get_attribute("alt"),
            }
        )

    metadata: list[dict[str, str | None]] = []
    meta_locator = page.locator("meta")
    for index in range(meta_locator.count()):
        meta = meta_locator.nth(index)
        metadata.append(
            {
                "name": meta.get_attribute("name"),
                "property": meta.get_attribute("property"),
                "content": meta.get_attribute("content"),
            }
        )

    raw_parameters = _raw_nodes(page, parameter_selectors)
    body_text = page.locator("body").inner_text()

    return {
        "url": requested_url,
        "title": _raw_first(
            page, ["h1", '[data-cy="ad_title"]', '[data-testid="ad-title"]']
        ),
        "price": _raw_first(
            page,
            [
                '[data-testid="ad-price-container"]',
                '[data-testid="ad-price"]',
                '[data-cy="ad_price"]',
            ],
        ),
        "location_and_date": _raw_first(
            page,
            [
                '[data-testid="location-date"]',
                '[data-testid="map-link"]',
                '[data-cy="ad_location"]',
            ],
        ),
        "description": _raw_first(
            page,
            [
                '[data-cy="ad_description"]',
                '[data-testid="ad-description"]',
                '[data-testid="ad-description-container"]',
            ],
        ),
        "seller": {
            "name": _raw_first(
                page,
                [
                    '[data-testid="user-profile-link"]',
                    '[data-testid="user-profile-name"]',
                ],
            ),
            "profile_url": _raw_first(
                page,
                [
                    'a[data-testid="user-profile-link"]',
                    '[data-testid="user-profile-link"] a',
                    'a[href*="/oferta/user/"]',
                    'a[href*="/profile/"]',
                ],
                "href",
            ),
            "phone": _raw_first(page, phone_selectors, "href")
            or _raw_first(page, phone_selectors),
        },
        # Сохраняем как исходные блоки, а не как нормализованный словарь:
        # одинаковые названия параметров не будут потеряны.
        "parameters": raw_parameters,
        "housing": {"raw_parameter_blocks": raw_parameters},
        "amenities": _raw_nodes(page, amenities_selectors),
        "nearby": _raw_nodes(page, nearby_selectors),
        "images": images,
        "meta": metadata,
        "body_text": body_text,
    }


def collect_raw_page(url: str, *, headed: bool, wait_ms: int) -> dict[str, Any]:
    """Load one page and return an unfiltered capture of its JSON payloads."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise SystemExit(
            "Не найден Playwright. Установите его командами: "
            "pip install playwright && playwright install chromium"
        ) from error

    json_responses: list[dict[str, Any]] = []
    response_errors: list[dict[str, str]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        page = browser.new_page(
            locale="ru-RU",
            viewport={"width": 1440, "height": 1000},
        )

        def capture_response(response: Any) -> None:
            try:
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type.lower():
                    return
                body = response.body().decode("utf-8", errors="replace")
                json_responses.append(
                    {
                        "url": response.url,
                        "status": response.status,
                        "content_type": content_type,
                        "body": body,
                    }
                )
            except Exception as error:  # A response may disappear during navigation.
                response_errors.append(
                    {"url": response.url, "error": str(error)}
                )

        page.on("response", capture_response)
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(max(0, wait_ms))
            html = page.content()
            final_url = page.url
            status = response.status if response else None

            json_scripts: list[dict[str, str]] = []
            scripts = page.locator("script")
            for index in range(scripts.count()):
                script = scripts.nth(index)
                script_type = script.get_attribute("type") or ""
                text = script.text_content() or ""
                if "json" in script_type.lower() or text.lstrip().startswith(
                    ("{", "[")
                ):
                    json_scripts.append(
                        {
                            "index": str(index + 1),
                            "type": script_type,
                            "raw": text,
                        }
                    )

            return {
                "requested_url": url,
                "final_url": final_url,
                "document_status": status,
                "publication": _publication_sections(page, url),
                "json_responses": json_responses,
                "json_scripts": json_scripts,
                "response_errors": response_errors,
                "html": html,
            }
        finally:
            browser.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _arguments()
    result = collect_raw_page(
        args.url,
        headed=args.headed,
        wait_ms=args.wait_ms,
    )
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
        print(f"Сырые данные сохранены: {args.output}")
    else:
        sys.stdout.write(output)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
