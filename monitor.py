#!/usr/bin/env python3
"""Monitor the public Chrome Hearts catalog and send Bark notifications.

The script intentionally uses only Python's standard library so it can run on
GitHub Actions or a small server without installing dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


BASE_URL = "https://www.chromehearts.com/"
DEFAULT_CATEGORY_URLS = (
    "https://www.chromehearts.com/baccarat",
    "https://www.chromehearts.com/scents",
    "https://www.chromehearts.com/boxers-leggings",
    "https://www.chromehearts.com/intimates",
    "https://www.chromehearts.com/socks",
)
DEFAULT_STATE_PATH = Path(__file__).resolve().parent / "data" / "state.json"
STATE_VERSION = 1
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
    "ChromeHeartsReleaseMonitor/1.0"
)
PRICE_RE = re.compile(
    r"\$[\d,]+(?:\.\d{2})?(?:\s*[-–]\s*\$?[\d,]+(?:\.\d{2})?)?"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_z(value: datetime | None = None) -> str:
    value = value or utc_now()
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def compact_text(value: str | None) -> str:
    return " ".join((value or "").split())


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalise_price(value: str) -> str:
    value = compact_text(value)
    value = re.sub(r"\.00(?=\s*(?:$|[-–]))", "", value)
    return value


def normalise_image_url(value: str, page_url: str) -> str:
    if not value:
        return ""
    absolute = urljoin(page_url, value)
    parts = urlsplit(absolute)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    # A smaller image is faster and more reliable inside an iPhone notification.
    if "/dw/image/" in parts.path:
        query["sw"] = "600"
        query["sh"] = "750"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


class ProductParser(HTMLParser):
    """Extract product cards from Chrome Hearts category HTML."""

    def __init__(self, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.products: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.product_div_depth = 0

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}
        classes = set(attrs.get("class", "").split())

        if (
            self.current is None
            and tag == "div"
            and "product" in classes
            and attrs.get("data-pid")
        ):
            self.current = {
                "id": attrs["data-pid"].strip(),
                "name": "",
                "price": "",
                "category": "",
                "url": "",
                "image": "",
                "available": True,
                "variants": set(),
                "text": [],
            }
            self.product_div_depth = 1
            return

        if self.current is None:
            return

        if tag == "div":
            self.product_div_depth += 1

        if "product-metadata" in classes:
            self.current["name"] = compact_text(attrs.get("data-name"))
            self.current["price"] = normalise_price(attrs.get("data-price", ""))
            self.current["category"] = compact_text(attrs.get("data-category"))

        if tag == "a" and "pdp-link-image" in classes and not self.current["url"]:
            self.current["url"] = urljoin(self.page_url, attrs.get("href", ""))

        if tag == "img" and "tile-image" in classes and not self.current["image"]:
            self.current["image"] = normalise_image_url(
                attrs.get("src", ""), self.page_url
            )
            if not self.current["name"]:
                self.current["name"] = compact_text(attrs.get("alt"))

        if tag == "button" and "swatch-attribute" in classes:
            variant = compact_text(attrs.get("data-swatchid"))
            if variant:
                self.current["variants"].add(variant)

        if "soldout" in {item.lower() for item in classes}:
            self.current["available"] = False

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        text = compact_text(data)
        if not text:
            return
        self.current["text"].append(text)
        upper = text.upper()
        if "OUT OF STOCK" in upper or "SOLD OUT" in upper:
            self.current["available"] = False

    def handle_endtag(self, tag: str) -> None:
        if self.current is None or tag != "div":
            return
        self.product_div_depth -= 1
        if self.product_div_depth == 0:
            self._finish_product()

    def close(self) -> None:
        super().close()
        # Be tolerant of a truncated final card, but only if it has useful data.
        if self.current is not None:
            self._finish_product()

    def _finish_product(self) -> None:
        assert self.current is not None
        item = self.current
        joined_text = " ".join(item.pop("text", []))
        if not item["price"]:
            match = PRICE_RE.search(joined_text)
            item["price"] = normalise_price(match.group(0)) if match else "价格待定"
        if not item["category"]:
            item["category"] = urlsplit(self.page_url).path.strip("/") or "官网"
        item["variants"] = sorted(item["variants"])
        item["source_page"] = self.page_url

        if item["id"] and item["name"] and item["url"]:
            self.products.append(item)
        self.current = None
        self.product_div_depth = 0


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attrs = {key: value or "" for key, value in attrs_list}
        if attrs.get("href"):
            self.links.append(attrs["href"])


def parse_products(page_url: str, document: str) -> list[dict[str, Any]]:
    parser = ProductParser(page_url)
    parser.feed(document)
    parser.close()
    return parser.products


def discover_category_urls(document: str) -> set[str]:
    """Discover current extensionless shop pages linked from the homepage."""
    parser = LinkParser()
    parser.feed(document)
    parser.close()
    discovered: set[str] = set()
    blocked = {
        "/account",
        "/bag",
        "/cart",
        "/checkout",
        "/contact",
        "/login",
        "/search",
    }
    for href in parser.links:
        absolute = urljoin(BASE_URL, href)
        parts = urlsplit(absolute)
        host = parts.netloc.lower().removeprefix("www.")
        path = parts.path.rstrip("/")
        if parts.scheme not in {"http", "https"} or host != "chromehearts.com":
            continue
        if not path or path == "/" or "." in path.rsplit("/", 1)[-1]:
            continue
        if any(path == prefix or path.startswith(prefix + "/") for prefix in blocked):
            continue
        # Current category pages are top-level; keeping the crawl at one level
        # prevents accidental visits to account or checkout routes.
        if path.count("/") != 1:
            continue
        discovered.add(urlunsplit(("https", "www.chromehearts.com", path, "", "")))
    return discovered


def fetch_text(url: str, attempts: int = 3, timeout: int = 25) -> str:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(5_000_001)
                if len(raw) > 5_000_000:
                    raise RuntimeError(f"Response too large: {url}")
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except HTTPError as exc:
            last_error = exc
            if exc.code < 500 and exc.code != 429:
                break
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"Unable to fetch {url}: {last_error}")


def configured_category_urls() -> set[str]:
    raw = os.environ.get("MONITOR_URLS", "")
    if not raw.strip():
        return set(DEFAULT_CATEGORY_URLS)
    return {item.strip() for item in raw.split(",") if item.strip()}


def collect_catalog() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    homepage = fetch_text(BASE_URL)
    urls = configured_category_urls() | discover_category_urls(homepage)
    pages: dict[str, str] = {BASE_URL: homepage}
    errors: list[str] = []
    delay = max(0.0, float(os.environ.get("CRAWL_DELAY_SECONDS", "0.35")))

    for url in sorted(urls):
        try:
            if delay:
                time.sleep(delay)
            pages[url] = fetch_text(url)
        except RuntimeError as exc:
            errors.append(str(exc))

    products: dict[str, dict[str, Any]] = {}
    page_counts: dict[str, int] = {}
    for url, document in pages.items():
        parsed = parse_products(url, document)
        page_counts[url] = len(parsed)
        for item in parsed:
            existing = products.get(item["id"])
            if existing is None or len(item.get("variants", [])) > len(
                existing.get("variants", [])
            ):
                products[item["id"]] = item

    if not products:
        detail = f" ({'; '.join(errors)})" if errors else ""
        raise RuntimeError(f"No products were found on the monitored pages{detail}")

    report = {
        "pages_checked": len(pages),
        "page_counts": page_counts,
        "errors": errors,
    }
    return products, report


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"State file is invalid: {exc}") from exc
    if data.get("version") != STATE_VERSION or not isinstance(data.get("products"), dict):
        raise RuntimeError("State file has an unsupported format")
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def product_for_state(item: dict[str, Any], now: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    record = {
        "id": item["id"],
        "name": item["name"],
        "price": item["price"],
        "category": item["category"],
        "url": item["url"],
        "image": item.get("image", ""),
        "available": bool(item.get("available", True)),
        "present": True,
        "variants": list(item.get("variants", [])),
        "source_page": item.get("source_page", ""),
        "first_seen_at": (previous or {}).get("first_seen_at", now),
    }
    comparable_previous = {
        key: value
        for key, value in (previous or {}).items()
        if key not in {"first_seen_at", "last_changed_at"}
    }
    comparable_record = {
        key: value for key, value in record.items() if key != "first_seen_at"
    }
    record["last_changed_at"] = (
        (previous or {}).get("last_changed_at", now)
        if comparable_previous == comparable_record
        else now
    )
    return record


def detect_events(
    state: dict[str, Any],
    current: dict[str, dict[str, Any]],
    notify_restocks: bool = False,
) -> list[dict[str, Any]]:
    known = state.get("products", {})
    events: list[dict[str, Any]] = []
    for product_id, item in current.items():
        previous = known.get(product_id)
        if previous is None:
            events.append({"type": "new", "product": item})
            continue
        was_unavailable = not previous.get("available", True) or not previous.get(
            "present", True
        )
        if notify_restocks and was_unavailable and item.get("available", True):
            events.append({"type": "restock", "product": item})
    return sorted(
        events,
        key=lambda event: (
            event["type"],
            event["product"].get("category", ""),
            event["product"].get("name", ""),
        ),
    )


def bark_config() -> tuple[str, str] | None:
    device_key = os.environ.get("BARK_DEVICE_KEY", "").strip()
    server = os.environ.get("BARK_SERVER", "https://api.day.app").strip().rstrip("/")
    copied_url = os.environ.get("BARK_URL", "").strip()

    if copied_url:
        parts = urlsplit(copied_url)
        path_parts = [part for part in parts.path.split("/") if part]
        if parts.scheme not in {"http", "https"} or not parts.netloc or not path_parts:
            raise RuntimeError("BARK_URL should look like https://api.day.app/YOUR_KEY/")
        device_key = path_parts[-1]
        prefix = "/" + "/".join(path_parts[:-1]) if len(path_parts) > 1 else ""
        server = urlunsplit((parts.scheme, parts.netloc, prefix, "", "")).rstrip("/")

    if not device_key:
        return None
    endpoint = server if server.endswith("/push") else server + "/push"
    return endpoint, device_key


def send_bark(
    title: str,
    body: str,
    *,
    subtitle: str = "",
    target_url: str = "",
    image: str = "",
    attempts: int = 3,
) -> None:
    config = bark_config()
    if config is None:
        raise RuntimeError("Bark is not configured; add BARK_URL or BARK_DEVICE_KEY")
    endpoint, device_key = config
    payload: dict[str, Any] = {
        "device_key": device_key,
        "title": title,
        "body": body,
        "group": "Chrome Hearts",
        "level": "timeSensitive",
        "isArchive": "1",
    }
    if subtitle:
        payload["subtitle"] = subtitle
    if target_url:
        payload["url"] = target_url
    if image:
        payload["image"] = image

    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = Request(
            endpoint,
            data=encoded,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                response_data = response.read(100_000).decode("utf-8", errors="replace")
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"Bark returned HTTP {response.status}")
                try:
                    result = json.loads(response_data)
                except json.JSONDecodeError:
                    result = {}
                if result.get("code") not in {None, 200}:
                    raise RuntimeError(f"Bark rejected the push: code {result.get('code')}")
                return
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"Bark push failed: {last_error}")


def build_initial_state(current: dict[str, dict[str, Any]], now: str) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "initialized_at": now,
        "last_heartbeat_at": now,
        "products": {
            product_id: product_for_state(item, now)
            for product_id, item in sorted(current.items())
        },
    }


def merge_state(
    state: dict[str, Any],
    current: dict[str, dict[str, Any]],
    accepted_event_keys: set[tuple[str, str]],
    attempted_event_keys: set[tuple[str, str]] | None = None,
    *,
    catalog_complete: bool,
    now: str,
) -> dict[str, Any]:
    known: dict[str, Any] = state["products"]
    current_ids = set(current)
    attempted_event_keys = attempted_event_keys or set()

    for product_id, item in current.items():
        previous = known.get(product_id)
        if previous is None and ("new", product_id) not in accepted_event_keys:
            # Keep it unknown so a failed Bark push is retried on the next run.
            continue
        if (
            previous is not None
            and ("restock", product_id) in attempted_event_keys
            and ("restock", product_id) not in accepted_event_keys
            and (not previous.get("available", True) or not previous.get("present", True))
            and item.get("available", True)
        ):
            # Preserve the old availability when a requested restock push failed.
            continue
        known[product_id] = product_for_state(item, now, previous)

    if catalog_complete:
        for product_id, previous in known.items():
            if product_id not in current_ids and previous.get("present", True):
                previous["present"] = False
                previous["last_changed_at"] = now

    last_heartbeat = parse_iso(state.get("last_heartbeat_at"))
    if last_heartbeat is None or utc_now() - last_heartbeat >= timedelta(days=30):
        state["last_heartbeat_at"] = now
    return state


def event_notification(event: dict[str, Any]) -> tuple[str, str, str]:
    item = event["product"]
    if event["type"] == "restock":
        title = "Chrome Hearts 官网补货"
    else:
        title = "Chrome Hearts 官网新品"
    availability = "可购买" if item.get("available", True) else "暂时售罄"
    subtitle = " · ".join(
        part for part in [item.get("category", ""), item.get("price", ""), availability] if part
    )
    return title, item["name"], subtitle


def print_summary(current: dict[str, Any], report: dict[str, Any], events: Iterable[dict[str, Any]] = ()) -> None:
    events = list(events)
    print(
        f"Checked {report['pages_checked']} pages; found {len(current)} products; "
        f"events: {len(events)}; page errors: {len(report['errors'])}"
    )
    for event in events:
        item = event["product"]
        print(f"- {event['type']}: {item['id']} | {item['name']} | {item['price']}")
    for error in report["errors"]:
        print(f"WARNING: {error}", file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    state_path = Path(args.state).expanduser().resolve()

    if args.test_bark:
        send_bark(
            "Chrome Hearts 监控测试成功",
            "你的 iPhone 已经可以接收新品提醒。",
            subtitle="点击可打开 Chrome Hearts 官网",
            target_url=BASE_URL,
        )
        print("Bark test push sent successfully")
        return 0

    current, report = collect_catalog()
    now = isoformat_z()
    state = None if args.reset_baseline else load_state(state_path)

    if state is None:
        print_summary(current, report)
        if args.dry_run:
            print("Dry run: baseline was not written")
            return 0
        state = build_initial_state(current, now)
        save_state(state_path, state)
        if bark_config() is not None:
            send_bark(
                "Chrome Hearts 监控已启动",
                f"已记录 {len(current)} 件现有商品；之后只提醒新出现的商品。",
                subtitle=f"每次检查 {report['pages_checked']} 个官网页面",
                target_url=BASE_URL,
            )
        print(f"Baseline created with {len(current)} products")
        return 0

    notify_restocks = env_flag("NOTIFY_RESTOCKS", False)
    events = detect_events(state, current, notify_restocks=notify_restocks)
    print_summary(current, report, events)
    if args.dry_run:
        print(json.dumps(events, ensure_ascii=False, indent=2))
        return 0

    if events and bark_config() is None:
        raise RuntimeError("New events detected, but Bark is not configured")

    accepted: set[tuple[str, str]] = set()
    failures: list[str] = []
    for event in events:
        item = event["product"]
        title, body, subtitle = event_notification(event)
        try:
            send_bark(
                title,
                body,
                subtitle=subtitle,
                target_url=item["url"],
                image=item.get("image", ""),
            )
            accepted.add((event["type"], item["id"]))
            time.sleep(0.4)
        except RuntimeError as exc:
            failures.append(f"{item['id']}: {exc}")

    merge_state(
        state,
        current,
        accepted,
        attempted_event_keys={
            (event["type"], event["product"]["id"]) for event in events
        },
        catalog_complete=not report["errors"],
        now=now,
    )
    save_state(state_path, state)

    if failures:
        raise RuntimeError("Some notifications failed; they will be retried: " + "; ".join(failures))
    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        default=str(DEFAULT_STATE_PATH),
        help="Path to the persistent state JSON file",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch and compare without pushing or saving")
    parser.add_argument("--test-bark", action="store_true", help="Send one test notification and exit")
    parser.add_argument(
        "--reset-baseline",
        action="store_true",
        help="Replace state with the current catalog without treating it as new",
    )
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
