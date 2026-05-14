#!/usr/bin/env python3
"""Pick a random image from a public web page.

This script is intentionally site-agnostic: it does not contain rules for any
specific website. It extracts common HTML image patterns, resolves relative
URLs, optionally checks that candidates are real images, then chooses one.
"""

from __future__ import annotations

import argparse
import curses
import html.parser
import os
import random
import re
import shutil
import sys
import time
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_TIMEOUT = 12
DEFAULT_PAGE_SAMPLE_LIMIT = 8
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
IMAGE_EXTENSIONS = {
    ".apng",
    ".avif",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".webp",
}
IMAGE_CONTENT_PREFIXES = ("image/",)
MIN_CONTENT_IMAGE_WIDTH = 64
MIN_CONTENT_IMAGE_HEIGHT = 64
MIN_CONTENT_IMAGE_AREA = 4096
RENDER_SETTLE_SECONDS = 1.5
NON_CONTENT_KEYWORDS = (
    "avatar",
    "badge",
    "button",
    "captcha",
    "favicon",
    "footer",
    "header",
    "icon",
    "logo",
    "placeholder",
    "search",
    "sprite",
    "symbol",
    "tracking",
    "transparent",
    "wordmark",
)
PAGE_LINK_SKIP_PREFIXES = (
    "data:",
    "blob:",
    "javascript:",
    "mailto:",
    "tel:",
)
PAGE_LINK_SKIP_EXTENSIONS = IMAGE_EXTENSIONS | {
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".ico",
    ".js",
    ".json",
    ".pdf",
    ".rss",
    ".txt",
    ".xml",
    ".zip",
}
LAZY_IMAGE_ATTRS = (
    "src",
    "data-src",
    "data-original",
    "data-lazy-src",
    "data-image",
    "data-url",
    "poster",
)
SRCSET_ATTRS = ("srcset", "data-srcset")
META_IMAGE_KEYS = {
    "og:image",
    "og:image:url",
    "og:image:secure_url",
    "twitter:image",
    "twitter:image:src",
}
ProgressCallback = Callable[[str, int, int], None]
COLOR_TITLE = 1
COLOR_STATUS = 2
COLOR_PROGRESS_FILL = 3
COLOR_PROGRESS_EMPTY = 4
COLOR_ERROR = 5
COLOR_RESULT = 6
COLOR_LOGO = 7
PENGUIN_LOGO = (
    "  .--.  ",
    " |o_o | ",
    " |:_/ | ",
    "//   \\ \\",
    "(|     |)",
    "/'\\_   _/`\\",
    "\\___)=(___/",
)
ANSI_RESET = "\033[0m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_BLUE = "\033[34m"
ANSI_YELLOW = "\033[33m"
PLAIN_PROGRESS_ROWS = 0


@dataclass(frozen=True)
class ImageCandidate:
    url: str
    source: str
    width: int | None = None
    height: int | None = None
    alt: str = ""


@dataclass(frozen=True)
class RenderedPage:
    html: str
    image_candidates: list[ImageCandidate]


class RandomImageError(RuntimeError):
    """Raised when a random image cannot be selected."""


class TerminalExit(RuntimeError):
    """Raised when the user exits the terminal UI."""


class ImageHTMLParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag.lower(), {key.lower(): value or "" for key, value in attrs}))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def normalize_page_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        return f"https://{url}"
    return url


def request_headers(accept: str = "*/*") -> dict[str, str]:
    return {"User-Agent": DEFAULT_USER_AGENT, "Accept": accept}


def auto_scroll_page(page: Any) -> None:
    page.evaluate(
        """
        async () => {
            const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
            const viewportHeight = window.innerHeight || 900;
            const maxScrolls = 4;

            for (let index = 0; index < maxScrolls; index += 1) {
                window.scrollBy(0, viewportHeight);
                await delay(350);
            }
            window.scrollTo(0, 0);
        }
        """
    )


def extract_dom_image_records(page: Any) -> list[dict[str, Any]]:
    return page.evaluate(
        """
        () => {
            const records = [];
            const pushRecord = (record) => {
                if (record && record.src) {
                    records.push(record);
                }
            };

            const parseSrcset = (value) => {
                if (!value) {
                    return [];
                }
                return value
                    .split(',')
                    .map(item => item.trim().split(/\\s+/)[0])
                    .filter(Boolean);
            };

            document.querySelectorAll('img').forEach(img => {
                const width = img.naturalWidth || img.width || null;
                const height = img.naturalHeight || img.height || null;
                const alt = img.alt || '';
                const attrs = [
                    ['currentSrc', img.currentSrc],
                    ['src', img.src],
                    ['data-src', img.getAttribute('data-src')],
                    ['data-original', img.getAttribute('data-original')],
                    ['data-lazy-src', img.getAttribute('data-lazy-src')],
                    ['data-image', img.getAttribute('data-image')],
                    ['data-url', img.getAttribute('data-url')],
                ];

                attrs.forEach(([source, src]) => {
                    pushRecord({src, width, height, alt, source: `img.${source}`});
                });

                ['srcset', 'data-srcset'].forEach(attr => {
                    parseSrcset(img.getAttribute(attr)).forEach(src => {
                        pushRecord({src, width, height, alt, source: `img[${attr}]`});
                    });
                });
            });

            document.querySelectorAll('source').forEach(sourceElement => {
                ['srcset', 'data-srcset'].forEach(attr => {
                    parseSrcset(sourceElement.getAttribute(attr)).forEach(src => {
                        pushRecord({
                            src,
                            width: null,
                            height: null,
                            alt: '',
                            source: `source[${attr}]`,
                        });
                    });
                });
            });

            document.querySelectorAll('*').forEach(element => {
                const backgroundImage = getComputedStyle(element).backgroundImage || '';
                if (!backgroundImage || backgroundImage === 'none') {
                    return;
                }
                const matches = backgroundImage.matchAll(/url\\((['"]?)(.*?)\\1\\)/g);
                for (const match of matches) {
                    const rect = element.getBoundingClientRect();
                    pushRecord({
                        src: match[2],
                        width: Math.round(rect.width) || null,
                        height: Math.round(rect.height) || null,
                        alt: element.getAttribute('aria-label') || '',
                        source: 'css.background-image',
                    });
                }
            });

            return records;
        }
        """
    )


def image_candidates_from_dom_records(
    records: list[dict[str, Any]], page_url: str
) -> list[ImageCandidate]:
    found: dict[str, ImageCandidate] = {}

    for record in records:
        image_url = clean_image_url(str(record.get("src") or ""), page_url)
        if not image_url or image_url in found:
            continue

        width = record.get("width")
        height = record.get("height")
        found[image_url] = ImageCandidate(
            url=image_url,
            source=str(record.get("source") or "dom"),
            width=width if isinstance(width, int) and width > 0 else None,
            height=height if isinstance(height, int) and height > 0 else None,
            alt=str(record.get("alt") or ""),
        )

    return list(found.values())


def fetch_rendered_page(page_url: str, timeout: int) -> RenderedPage:
    timeout_ms = max(1, timeout) * 1000
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=DEFAULT_USER_AGENT,
                    extra_http_headers=request_headers(
                        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                    ),
                    viewport={"width": 1366, "height": 900},
                )
                page = context.new_page()
                response = page.goto(
                    page_url, wait_until="domcontentloaded", timeout=timeout_ms
                )
                if response:
                    content_type = (response.headers.get("content-type") or "").lower()
                    if content_type and "html" not in content_type and "xml" not in content_type:
                        raise RandomImageError(
                            "The URL did not return an HTML page. "
                            f"Content-Type: {content_type or 'unknown'}"
                        )

                try:
                    page.wait_for_load_state("networkidle", timeout=timeout_ms)
                except PlaywrightTimeoutError:
                    pass

                auto_scroll_page(page)
                page.wait_for_timeout(int(RENDER_SETTLE_SECONDS * 1000))
                records = extract_dom_image_records(page)
                html = page.content()
            finally:
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise TimeoutError(str(exc)) from exc
    except PlaywrightError as exc:
        raise RandomImageError(
            "Playwright could not render the page. Make sure Chromium and its "
            "native dependencies are installed with `playwright install chromium` "
            "and, on Linux/WSL, `playwright install-deps chromium`."
        ) from exc

    return RenderedPage(
        html=html,
        image_candidates=image_candidates_from_dom_records(records, page_url),
    )


def fetch_html(page_url: str, timeout: int) -> str:
    return fetch_rendered_page(page_url, timeout).html


def parse_srcset(value: str) -> Iterable[str]:
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        yield item.split()[0]


def clean_image_url(url: str, base_url: str) -> str | None:
    url = url.strip()
    if not url or url.startswith(("data:", "blob:", "javascript:", "mailto:")):
        return None
    absolute_url = urljoin(base_url, url)
    parsed = urlparse(absolute_url)
    if parsed.scheme not in {"http", "https"}:
        return None
    return absolute_url


def image_extension_score(url: str) -> bool:
    path = unquote(urlparse(url).path).lower()
    _, ext = os.path.splitext(path)
    return ext in IMAGE_EXTENSIONS


def parse_dimension(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    if not match:
        return None
    return int(match.group(0))


def root_domain(hostname: str) -> str:
    parts = hostname.lower().split(".")
    if len(parts) <= 2:
        return hostname.lower()
    return ".".join(parts[-2:])


def same_site(url: str, page_url: str) -> bool:
    url_host = urlparse(url).hostname or ""
    page_host = urlparse(page_url).hostname or ""
    return bool(url_host and page_host and root_domain(url_host) == root_domain(page_host))


def normalize_link_url(url: str, base_url: str) -> str | None:
    url = url.strip()
    if not url or url.startswith(PAGE_LINK_SKIP_PREFIXES):
        return None
    absolute_url = urljoin(base_url, url)
    parsed = urlparse(absolute_url)
    if parsed.scheme not in {"http", "https"}:
        return None
    path = unquote(parsed.path).lower()
    _, ext = os.path.splitext(path)
    if ext in PAGE_LINK_SKIP_EXTENSIONS:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def collect_page_links(html: str, page_url: str) -> list[str]:
    parser = ImageHTMLParser()
    parser.feed(html)
    links: list[str] = []
    seen: set[str] = {page_url}

    for tag_name, attrs in parser.tags:
        if tag_name != "a":
            continue
        link_url = normalize_link_url(attrs.get("href", ""), page_url)
        if not link_url or link_url in seen or not same_site(link_url, page_url):
            continue
        seen.add(link_url)
        links.append(link_url)

    random.shuffle(links)
    return links


def looks_like_non_content_image(candidate: ImageCandidate) -> bool:
    haystack = " ".join((candidate.url, candidate.source, candidate.alt)).lower()
    if any(keyword in haystack for keyword in NON_CONTENT_KEYWORDS):
        return True

    if candidate.width is not None and candidate.width < MIN_CONTENT_IMAGE_WIDTH:
        return True
    if candidate.height is not None and candidate.height < MIN_CONTENT_IMAGE_HEIGHT:
        return True
    if candidate.width is not None and candidate.height is not None:
        if candidate.width * candidate.height < MIN_CONTENT_IMAGE_AREA:
            return True

    return False


def content_image_score(candidate: ImageCandidate) -> int:
    parsed = urlparse(candidate.url)
    path = unquote(parsed.path).lower()
    _, ext = os.path.splitext(path)
    score = 0

    if candidate.width:
        score += min(candidate.width, 2000) // 100
    if candidate.height:
        score += min(candidate.height, 2000) // 100
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
        score += 8
    if ext == ".svg":
        score -= 6
    if "thumb" in path or "upload" in parsed.netloc:
        score += 5
    if candidate.source.startswith("meta"):
        score -= 4

    return score


def collect_image_candidates(html: str, page_url: str) -> list[ImageCandidate]:
    parser = ImageHTMLParser()
    parser.feed(html)
    base_url = page_url
    for tag_name, attrs in parser.tags:
        if tag_name == "base" and attrs.get("href"):
            base_url = urljoin(page_url, attrs["href"])
            break

    found: dict[str, ImageCandidate] = {}

    def add(
        raw_url: str | None,
        source: str,
        *,
        width: int | None = None,
        height: int | None = None,
        alt: str = "",
    ) -> None:
        if not raw_url:
            return
        image_url = clean_image_url(raw_url, base_url)
        if image_url and image_url not in found:
            found[image_url] = ImageCandidate(
                url=image_url,
                source=source,
                width=width,
                height=height,
                alt=alt,
            )

    for tag_name, attrs in parser.tags:
        if tag_name not in {"img", "source"}:
            continue
        width = parse_dimension(attrs.get("width"))
        height = parse_dimension(attrs.get("height"))
        alt = attrs.get("alt", "")
        for attr in LAZY_IMAGE_ATTRS:
            add(attrs.get(attr), f"{tag_name}[{attr}]", width=width, height=height, alt=alt)
        for attr in SRCSET_ATTRS:
            srcset = attrs.get(attr)
            if srcset:
                for srcset_url in parse_srcset(srcset):
                    add(srcset_url, f"{tag_name}[{attr}]", width=width, height=height, alt=alt)

    for tag_name, attrs in parser.tags:
        if tag_name != "meta":
            continue
        key = attrs.get("property") or attrs.get("name")
        if key and key.lower() in META_IMAGE_KEYS:
            add(attrs.get("content"), f"meta[{key}]")

    for _tag_name, attrs in parser.tags:
        if not attrs.get("style"):
            continue
        for match in re.finditer(r"url\((['\"]?)(.*?)\1\)", attrs.get("style", "")):
            add(match.group(2), "inline-style")

    return list(found.values())


def is_probably_image(candidate: ImageCandidate, timeout: int, validate: bool) -> bool:
    if image_extension_score(candidate.url) and not validate:
        return True

    if not validate:
        return True

    try:
        request = Request(
            candidate.url,
            headers=request_headers(),
            method="HEAD",
        )
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "").lower()
    except HTTPError as exc:
        if exc.code != 405:
            return False
        try:
            request = Request(candidate.url, headers=request_headers())
            with urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get("content-type", "").lower()
        except (HTTPError, URLError, TimeoutError):
            return False
    except (URLError, TimeoutError):
        return False

    return content_type.startswith(IMAGE_CONTENT_PREFIXES)


def collect_verified_image_candidates(
    page_url: str,
    *,
    validate: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    page_sample_limit: int = DEFAULT_PAGE_SAMPLE_LIMIT,
    progress_callback: ProgressCallback | None = None,
) -> list[ImageCandidate]:
    page_url = normalize_page_url(page_url)
    page_sample_limit = max(1, page_sample_limit)
    if progress_callback:
        progress_callback(f"Loading start page: {page_url}", 0, 1)
    start_page = fetch_rendered_page(page_url, timeout)
    if progress_callback:
        progress_callback(f"Loaded start page: {page_url}", 1, 1)
    pages = [
        page_url,
        *collect_page_links(start_page.html, page_url)[: page_sample_limit - 1],
    ]

    candidates: list[ImageCandidate] = []
    seen_images: set[str] = set()
    for index, sampled_page_url in enumerate(pages):
        if progress_callback:
            progress_callback(
                f"Scanning sampled page {index + 1}/{len(pages)}: {sampled_page_url}",
                index,
                len(pages),
            )
        try:
            rendered_page = (
                start_page
                if index == 0
                else fetch_rendered_page(sampled_page_url, timeout)
            )
        except (HTTPError, URLError, TimeoutError, RandomImageError):
            continue

        page_candidates = [
            *rendered_page.image_candidates,
            *collect_image_candidates(rendered_page.html, sampled_page_url),
        ]
        for candidate in page_candidates:
            if candidate.url in seen_images or looks_like_non_content_image(candidate):
                continue
            seen_images.add(candidate.url)
            candidates.append(candidate)
        if progress_callback:
            progress_callback(
                f"Finished sampled page {index + 1}/{len(pages)}: {sampled_page_url}",
                index + 1,
                len(pages),
            )

    valid_candidates: list[ImageCandidate] = []
    for index, candidate in enumerate(candidates):
        if progress_callback:
            progress_callback(
                f"Validating image candidate {index + 1}/{len(candidates)}: {candidate.url}",
                index,
                len(candidates),
            )
        if is_probably_image(candidate, timeout=timeout, validate=validate):
            valid_candidates.append(candidate)
        if progress_callback:
            progress_callback(
                f"Validated image candidate {index + 1}/{len(candidates)}: {candidate.url}",
                index + 1,
                len(candidates),
            )

    if not valid_candidates:
        raise RandomImageError(f"No content images were found on {page_url}")

    return valid_candidates


def choose_from_verified_images(candidates: list[ImageCandidate]) -> ImageCandidate:
    scores = [max(1, content_image_score(candidate)) for candidate in candidates]
    return random.choices(candidates, weights=scores, k=1)[0]


def choose_random_images(
    page_url: str,
    count: int,
    *,
    validate: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    page_sample_limit: int = DEFAULT_PAGE_SAMPLE_LIMIT,
    progress_callback: ProgressCallback | None = None,
) -> list[ImageCandidate]:
    verified_candidates = collect_verified_image_candidates(
        page_url,
        validate=validate,
        timeout=timeout,
        page_sample_limit=page_sample_limit,
        progress_callback=progress_callback,
    )
    selections: list[ImageCandidate] = []
    remaining = verified_candidates[:]
    for _ in range(min(count, len(remaining))):
        selected = choose_from_verified_images(remaining)
        selections.append(selected)
        remaining.remove(selected)
    return selections


def choose_random_image(
    page_url: str,
    *,
    validate: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    page_sample_limit: int = DEFAULT_PAGE_SAMPLE_LIMIT,
    progress_callback: ProgressCallback | None = None,
) -> ImageCandidate:
    return choose_random_images(
        page_url,
        1,
        validate=validate,
        timeout=timeout,
        page_sample_limit=page_sample_limit,
        progress_callback=progress_callback,
    )[0]


def filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    name = Path(path).name or "random-image"
    if "." not in name:
        name = f"{name}.img"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def download_image(
    image_url: str,
    output_dir: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / filename_from_url(image_url)

    request = Request(image_url, headers=request_headers())
    with urlopen(request, timeout=timeout) as response:
        try:
            total_bytes = int(response.headers.get("content-length") or 0)
        except ValueError:
            total_bytes = 0
        downloaded_bytes = 0
        if progress_callback:
            progress_callback(
                f"Downloading selected image: {image_url}",
                0,
                total_bytes or 1,
            )
        with destination.open("wb") as image_file:
            while True:
                chunk = response.read(1024 * 64)
                if not chunk:
                    break
                if chunk:
                    image_file.write(chunk)
                    downloaded_bytes += len(chunk)
                    if progress_callback:
                        progress_callback(
                            f"Downloading selected image: {image_url}",
                            downloaded_bytes if total_bytes else 1,
                            total_bytes or 1,
                        )

    return destination


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select a random image from any public webpage."
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="Page URL to inspect, for example example.com",
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=None,
        help="Number of random image URLs to print.",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip HEAD/GET validation of image candidates. Faster but less accurate.",
    )
    parser.add_argument(
        "--download",
        type=Path,
        help="Download the selected image(s) into this directory.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Network timeout in seconds. Default: {DEFAULT_TIMEOUT}.",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=DEFAULT_PAGE_SAMPLE_LIMIT,
        help=(
            "Number of same-site pages to sample before choosing an image. "
            f"Default: {DEFAULT_PAGE_SAMPLE_LIMIT}."
        ),
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Run in plain command-line mode instead of the full-screen terminal UI.",
    )
    return parser.parse_args(argv)


def draw_text(window: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = window.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    window.addnstr(y, x, text, max(0, width - x - 1), attr)


def init_terminal_colors() -> None:
    if not curses.has_colors():
        return
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(COLOR_TITLE, curses.COLOR_CYAN, -1)
        curses.init_pair(COLOR_STATUS, curses.COLOR_YELLOW, -1)
        curses.init_pair(COLOR_PROGRESS_FILL, curses.COLOR_GREEN, -1)
        curses.init_pair(COLOR_PROGRESS_EMPTY, curses.COLOR_BLUE, -1)
        curses.init_pair(COLOR_ERROR, curses.COLOR_RED, -1)
        curses.init_pair(COLOR_RESULT, curses.COLOR_MAGENTA, -1)
        curses.init_pair(COLOR_LOGO, curses.COLOR_WHITE, -1)
    except curses.error:
        pass


def color_attr(color_pair: int, extra: int = 0) -> int:
    if not curses.has_colors():
        return extra
    return curses.color_pair(color_pair) | extra


def draw_progress_line(
    window: curses.window,
    y: int,
    x: int,
    width: int,
    completed: int,
    total: int,
) -> None:
    width = max(0, width)
    if width <= 0:
        return
    completed = max(0, completed)
    total = max(1, total)
    filled = round(width * min(completed, total) / total)
    if filled:
        draw_text(window, y, x, "=" * filled, color_attr(COLOR_PROGRESS_FILL, curses.A_BOLD))
    if filled < width:
        draw_text(window, y, x + filled, "-" * (width - filled), color_attr(COLOR_PROGRESS_EMPTY))


def draw_progress_bar(
    window: curses.window,
    y: int,
    x: int,
    width: int,
    completed: int,
    total: int,
) -> None:
    width = max(10, width)
    completed = max(0, completed)
    total = max(1, total)
    filled = round(width * min(completed, total) / total)
    percent = round(100 * min(completed, total) / total)
    draw_text(window, y, x, "[")
    if filled:
        draw_text(
            window,
            y,
            x + 1,
            "#" * filled,
            color_attr(COLOR_PROGRESS_FILL, curses.A_BOLD),
        )
    if filled < width:
        draw_text(
            window,
            y,
            x + 1 + filled,
            "-" * (width - filled),
            color_attr(COLOR_PROGRESS_EMPTY),
        )
    draw_text(window, y, x + width + 1, f"] {percent:3d}% {completed}/{total}")


def wrap_terminal_text(text: str, width: int) -> list[str]:
    if width <= 0:
        return []
    return textwrap.wrap(
        text,
        width=width,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [""]


def draw_penguin_logo(window: curses.window) -> None:
    height, width = window.getmaxyx()
    logo_width = max(len(line) for line in PENGUIN_LOGO)
    logo_height = len(PENGUIN_LOGO)
    if height < logo_height + 4 or width < logo_width + 8:
        return

    start_y = height - logo_height - 2
    start_x = width - logo_width - 3
    for offset, line in enumerate(PENGUIN_LOGO):
        draw_text(
            window,
            start_y + offset,
            start_x,
            line,
            color_attr(COLOR_LOGO, curses.A_BOLD),
        )


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    return f"{int(minutes)}m {remaining_seconds:.1f}s"


def supports_ansi_color() -> bool:
    return sys.stderr.isatty() and os.environ.get("NO_COLOR") is None


def ansi(text: str, color: str) -> str:
    if not supports_ansi_color():
        return text
    return f"{color}{text}{ANSI_RESET}"


def render_plain_progress(
    *,
    image_index: int,
    image_count: int,
    label: str,
    completed: int,
    total: int,
    elapsed: float,
) -> None:
    if not sys.stderr.isatty():
        return
    global PLAIN_PROGRESS_ROWS
    total = max(1, total)
    completed = max(0, min(completed, total))
    terminal_width = max(40, shutil.get_terminal_size((80, 20)).columns)
    bar_width = min(28, max(10, terminal_width - 32))
    filled = round(bar_width * completed / total)
    bar = (
        ansi("#" * filled, ANSI_GREEN)
        + ansi("-" * (bar_width - filled), ANSI_BLUE)
    )
    percent = round(100 * completed / total)
    if PLAIN_PROGRESS_ROWS:
        sys.stderr.write("\r\033[K")
        for _ in range(PLAIN_PROGRESS_ROWS - 1):
            sys.stderr.write("\033[1A\r\033[K")
    prefix = "Current line: "
    label_width = max(10, terminal_width - len(prefix))
    label_lines = wrap_terminal_text(label, label_width)
    lines = [
        (
            f"{ansi('Current progress:', ANSI_CYAN)} "
            f"image {image_index}/{image_count} | {percent:3d}% {completed}/{total} | "
            f"{ansi(format_duration(elapsed), ANSI_YELLOW)}"
        ),
        f"{prefix}{label_lines[0]}",
        *[f"{' ' * len(prefix)}{line}" for line in label_lines[1:]],
        f"[{bar}]",
    ]
    sys.stderr.write("\n".join(lines))
    sys.stderr.flush()
    PLAIN_PROGRESS_ROWS = len(lines)


def clear_plain_progress_line() -> None:
    global PLAIN_PROGRESS_ROWS
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K")
        for _ in range(max(0, PLAIN_PROGRESS_ROWS - 1)):
            sys.stderr.write("\033[1A\r\033[K")
        sys.stderr.flush()
    PLAIN_PROGRESS_ROWS = 0


def render_frame(
    window: curses.window,
    *,
    title: str = "Random Image Picker",
    status: str = "",
    progress: tuple[int, int] | None = None,
    sub_progress: tuple[str, int, int] | None = None,
    timing: str = "",
    results: list[str] | None = None,
) -> None:
    window.erase()
    height, width = window.getmaxyx()
    window.border()
    content_width = max(10, width - 6)
    draw_text(window, 1, 3, title, color_attr(COLOR_TITLE, curses.A_BOLD))
    draw_progress_line(window, 2, 3, content_width, 1, 1)
    if status:
        status_attr = color_attr(COLOR_ERROR if status.lower().startswith("error:") else COLOR_STATUS)
        draw_text(window, 3, 3, status, status_attr)
    if timing:
        draw_text(window, 4, 3, timing)
    current_label = ""
    current_completed = 0
    current_total = 1
    if sub_progress:
        current_label, current_completed, current_total = sub_progress
    elif progress:
        current_completed, current_total = progress
        current_label = "Total progress"
    if current_label:
        percent = round(100 * min(max(0, current_completed), max(1, current_total)) / max(1, current_total))
        draw_text(
            window,
            5,
            3,
            f"Current progress: {percent}%",
            color_attr(COLOR_PROGRESS_FILL, curses.A_BOLD),
        )
        current_line_prefix = "Current line: "
        max_current_lines = max(1, height - 18)
        label_width = max(10, content_width - len(current_line_prefix))
        current_lines = wrap_terminal_text(current_label, label_width)[:max_current_lines]
        for offset, line in enumerate(current_lines):
            prefix = current_line_prefix if offset == 0 else " " * len(current_line_prefix)
            draw_text(
                window,
                6 + offset,
                3,
                f"{prefix}{line}",
                color_attr(COLOR_PROGRESS_FILL, curses.A_BOLD),
            )
        next_section_y = 6 + len(current_lines) + 1
    else:
        next_section_y = 7
    if progress:
        completed, total = progress
        draw_text(window, next_section_y, 3, "Total", color_attr(COLOR_TITLE, curses.A_BOLD))
        draw_progress_bar(window, next_section_y + 1, 3, max(10, width - 24), completed, total)
        next_section_y += 3
    if sub_progress:
        label, completed, total = sub_progress
        draw_text(window, next_section_y, 3, "Current item", color_attr(COLOR_TITLE, curses.A_BOLD))
        draw_progress_bar(window, next_section_y + 1, 3, max(10, width - 24), completed, total)
        next_section_y += 3
    if results:
        results_header_y = next_section_y if (sub_progress or progress or current_label) else 7
        draw_text(window, results_header_y, 3, "Images", color_attr(COLOR_TITLE, curses.A_BOLD))
        available_rows = max(0, height - results_header_y - 5)
        for row, url in enumerate(results[-available_rows:]):
            draw_text(window, results_header_y + 1 + row, 3, url, color_attr(COLOR_RESULT))
    draw_penguin_logo(window)
    draw_text(window, height - 2, 3, "Press q to exit", color_attr(COLOR_STATUS))
    window.refresh()


def wait_for_user_close(window: curses.window) -> None:
    while window.getch() not in (ord("q"), ord("Q"), 27, 10, 13):
        pass


def prompt_text(window: curses.window, prompt: str, *, default: str = "") -> str:
    value = default
    while True:
        render_frame(window, status=prompt)
        draw_text(window, 5, 3, "> ")
        draw_text(window, 5, 5, value)
        window.move(5, min(5 + len(value), window.getmaxyx()[1] - 2))
        key = window.getch()

        if key in (ord("q"), 27) and not value:
            raise TerminalExit()
        if key in (curses.KEY_ENTER, 10, 13) and value.strip():
            return value.strip()
        if key in (curses.KEY_BACKSPACE, 127, 8):
            value = value[:-1]
            continue
        if key == curses.KEY_RESIZE:
            continue
        if 32 <= key <= 126:
            value += chr(key)


def prompt_positive_int(
    window: curses.window,
    prompt: str,
    *,
    default: int | None = None,
) -> int:
    value = "" if default is None else str(default)
    error = ""
    while True:
        status = prompt if not error else f"{prompt}  {error}"
        render_frame(window, status=status)
        draw_text(window, 5, 3, "> ")
        draw_text(window, 5, 5, value)
        window.move(5, min(5 + len(value), window.getmaxyx()[1] - 2))
        key = window.getch()

        if key in (ord("q"), 27) and not value:
            raise TerminalExit()
        if key in (curses.KEY_ENTER, 10, 13):
            if value.isdigit() and int(value) >= 1:
                return int(value)
            error = "Enter a number of 1 or higher."
            continue
        if key in (curses.KEY_BACKSPACE, 127, 8):
            value = value[:-1]
            error = ""
            continue
        if key == curses.KEY_RESIZE:
            continue
        if ord("0") <= key <= ord("9"):
            value += chr(key)
            error = ""


def run_terminal_ui(args: argparse.Namespace) -> int:
    def run(window: curses.window) -> int:
        init_terminal_colors()
        curses.curs_set(1)
        window.keypad(True)
        url = args.url or prompt_text(window, "Enter the page URL to inspect")
        count = prompt_positive_int(
            window,
            "How many images do you want to get?",
            default=args.count,
        )

        curses.curs_set(0)
        results: list[str] = []
        image_times: list[float] = []
        start_time = time.perf_counter()

        def update_sub_progress(label: str, completed: int, total: int) -> None:
            render_frame(
                window,
                status=f"Finding verified images for {count} request(s)...",
                progress=(0, count),
                sub_progress=(label, completed, total),
                timing=f"Elapsed: {format_duration(time.perf_counter() - start_time)}",
                results=results,
            )

        render_frame(
            window,
            status=f"Finding verified images for {count} request(s)...",
            progress=(0, count),
            sub_progress=("Starting image search", 0, 1),
            results=results,
        )
        try:
            selections = choose_random_images(
                url,
                count,
                validate=not args.no_validate,
                timeout=args.timeout,
                page_sample_limit=args.pages,
                progress_callback=update_sub_progress,
            )
        except (HTTPError, URLError, TimeoutError, RandomImageError) as exc:
            render_frame(
                window,
                status=f"Error: {exc}",
                progress=(0, count),
                sub_progress=("Stopped on error", 0, 1),
                timing=f"Elapsed: {format_duration(time.perf_counter() - start_time)}",
                results=results,
            )
            wait_for_user_close(window)
            return 1

        for index, selected in enumerate(selections):
            image_start = time.perf_counter()
            result = selected.url
            try:
                if args.download:
                    destination = download_image(
                        selected.url,
                        args.download,
                        timeout=args.timeout,
                        progress_callback=update_sub_progress,
                    )
                    result = f"{selected.url} -> {destination}"
            except (HTTPError, URLError, TimeoutError, RandomImageError) as exc:
                render_frame(
                    window,
                    status=f"Error: {exc}",
                    progress=(index, count),
                    sub_progress=("Stopped on error", 0, 1),
                    timing=f"Current image: {format_duration(time.perf_counter() - image_start)}",
                    results=results,
                )
                wait_for_user_close(window)
                return 1
            results.append(result)
            image_times.append(time.perf_counter() - image_start)
            average_time = sum(image_times) / len(image_times)
            render_frame(
                window,
                status=f"Completed image {index + 1} of {count}.",
                progress=(index + 1, count),
                sub_progress=("Image complete", 1, 1),
                timing=(
                    f"Last image: {format_duration(image_times[-1])} | "
                    f"Average: {format_duration(average_time)}"
                ),
                results=results,
            )

        average_time = sum(image_times) / len(image_times) if image_times else 0.0
        render_frame(
            window,
            status=f"Done. Selected {len(results)} verified image(s).",
            progress=(count, count),
            timing=f"Average time per image: {format_duration(average_time)}",
            results=results,
        )
        wait_for_user_close(window)
        return 0

    try:
        return curses.wrapper(run)
    except TerminalExit:
        return 0
    except curses.error as exc:
        print(f"Error: terminal UI failed: {exc}", file=sys.stderr)
        return 1


def run_plain(args: argparse.Namespace) -> int:
    if not args.url:
        print(
            "Error: url is required when using --no-ui or a non-interactive terminal",
            file=sys.stderr,
        )
        return 1

    count = args.count or 1
    if count < 1:
        print("Error: --count must be at least 1", file=sys.stderr)
        return 1

    try:
        image_times: list[float] = []
        start_time = time.perf_counter()

        def update_plain_progress(label: str, completed: int, total: int) -> None:
            render_plain_progress(
                image_index=1,
                image_count=count,
                label=label,
                completed=completed,
                total=total,
                elapsed=time.perf_counter() - start_time,
            )

        selections = choose_random_images(
            args.url,
            count,
            validate=not args.no_validate,
            timeout=args.timeout,
            page_sample_limit=args.pages,
            progress_callback=update_plain_progress,
        )
        clear_plain_progress_line()
        for index, selected in enumerate(selections):
            image_start = time.perf_counter()
            print(selected.url)
            if args.download:
                destination = download_image(
                    selected.url,
                    args.download,
                    timeout=args.timeout,
                    progress_callback=update_plain_progress,
                )
                clear_plain_progress_line()
                print(f"Downloaded: {destination}")
            image_times.append(time.perf_counter() - image_start)
            average_time = sum(image_times) / len(image_times)
            print(
                (
                    f"Image {index + 1}/{count} time: "
                    f"{format_duration(image_times[-1])}; "
                    f"average: {format_duration(average_time)}"
                ),
                file=sys.stderr,
            )
    except (HTTPError, URLError, TimeoutError, RandomImageError) as exc:
        clear_plain_progress_line()
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.count is not None and args.count < 1:
        print("Error: --count must be at least 1", file=sys.stderr)
        return 1
    if args.no_ui or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return run_plain(args)
    return run_terminal_ui(args)


if __name__ == "__main__":
    raise SystemExit(main())
