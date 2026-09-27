"""Fetch a blocked product page before OpenAI runs.

httpx is the first attempt. When that page is a block screen, these steps
download HTML and the crawler scrapes the local file:

1. curl_cffi with a Chrome TLS fingerprint
2. a Wayback Machine copy
3. an archive.ph copy
4. Playwright
5. Scrapy
6. Crawl4AI, without an LLM

If those and the other-site search still fail, Exa live-crawls the URL.
BeautifulSoup parsing stays in extract.assemble_product. OpenAI runs after this.
"""

from __future__ import annotations

import hashlib
import html
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

import httpx

from product_crawler.blocked import blocked_note, cache_note, product_clues
from product_crawler.extract import assemble_product

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
CACHE_METHODS = {"wayback", "archive.ph"}
Fetcher = Callable[[], tuple[str, str, str] | None]


def document_is_blocked(html: str) -> bool:
    sample = (html or "")[:8000].lower()
    if "<title>access denied</title>" in sample or ("access denied" in sample and "edgesuite" in sample):
        return True
    if "continue shopping" in sample and ("not a robot" in sample or "click the button below" in sample):
        return True
    return False


def shell_title(title: str | None) -> bool:
    text = (title or "").strip().lower()
    return text in {"skip to", "robot check", "access denied", "continue shopping"} or text.startswith("skip to ")


def page_has_product(html: str, url: str) -> bool:
    if document_is_blocked(html):
        return False
    title = assemble_product(
        url,
        html,
        None,
        None,
        product_js_url=None,
        reviews_endpoint=None,
    ).get("title")
    return bool(title) and not shell_title(title)


def archive_target(url: str) -> str:
    """Product path without ad query parameters, so a cache lookup can match."""
    parsed = urlparse(url)
    if parsed.netloc.lower().removeprefix("www.").split(".")[0] == "amazon":
        return urlunparse(parsed._replace(query="", fragment=""))
    return url.split("#", 1)[0]


def write_local_copy(cache_dir: Path, url: str, html: str, method: str, source_url: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    path = cache_dir / f"{digest}.html"
    path.write_text(html, encoding="utf-8")
    (cache_dir / f"{digest}.json").write_text(
        json.dumps({"url": url, "method": method, "source_url": source_url}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def recover_blocked_page(
    url: str,
    client: httpx.Client,
    *,
    cache_dir: Path | None = None,
    fetchers: list[Fetcher] | None = None,
) -> tuple[str, str, str | None, str] | None:
    """Return local HTML, source URL, note, and method when a later tool gets the product."""
    folder = cache_dir or Path("cache/pages")
    steps = fetchers if fetchers is not None else default_fetchers(client, url)
    for fetch in steps:
        try:
            found = fetch()
        except Exception:
            continue
        if found is None:
            continue
        html, source_url, method = found
        saved = _accept(folder, url, html, source_url, method)
        if saved is not None:
            return saved
    return None


def default_fetchers(client: httpx.Client, url: str) -> list[Fetcher]:
    return [
        lambda: _live(fetch_curl_cffi(url), url, "curl_cffi"),
        lambda: _cached(fetch_wayback(client, url), "wayback"),
        lambda: _cached(fetch_archive_ph(client, url), "archive.ph"),
        lambda: _live(fetch_playwright(url), url, "playwright"),
        lambda: _live(fetch_scrapy(url), url, "scrapy"),
        lambda: _live(fetch_crawl4ai(url), url, "crawl4ai"),
    ]


def fetch_curl_cffi(url: str) -> str | None:
    from curl_cffi import requests

    response = requests.get(url, impersonate="chrome", timeout=45, allow_redirects=True)
    if response.status_code >= 400:
        return None
    return response.text


def fetch_playwright(url: str) -> str | None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            return page.content()
        finally:
            browser.close()


def fetch_scrapy(url: str) -> str | None:
    script = '''
import sys
from scrapy import Spider
from scrapy.crawler import CrawlerProcess

class OnePage(Spider):
    name = "one-page"
    custom_settings = {
        "LOG_ENABLED": False,
        "ROBOTSTXT_OBEY": False,
        "USER_AGENT": %r,
        "DOWNLOAD_TIMEOUT": 40,
    }

    def __init__(self, target, dest, **kwargs):
        super().__init__(**kwargs)
        self.start_urls = [target]
        self.dest = dest

    def parse(self, response):
        open(self.dest, "w", encoding="utf-8").write(response.text)

process = CrawlerProcess()
process.crawl(OnePage, target=sys.argv[1], dest=sys.argv[2])
process.start()
''' % USER_AGENT
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "page.html"
        script_path = Path(tmp) / "fetch.py"
        script_path.write_text(script, encoding="utf-8")
        try:
            completed = subprocess.run(
                [sys.executable, str(script_path), url, str(dest)],
                capture_output=True,
                timeout=70,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0 or not dest.is_file():
            return None
        return dest.read_text(encoding="utf-8", errors="replace")


def fetch_crawl4ai(url: str) -> str | None:
    import asyncio

    from crawl4ai import AsyncWebCrawler

    async def _run() -> str | None:
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)
        html = getattr(result, "html", None)
        return html if isinstance(html, str) and html.strip() else None

    return asyncio.run(_run())


def fetch_wayback(client: httpx.Client, url: str) -> tuple[str, str] | None:
    target = archive_target(url)
    try:
        listing = client.get(
            "https://web.archive.org/cdx/search/cdx",
            params={
                "url": target,
                "output": "json",
                "fl": "timestamp,original,statuscode",
                "filter": "statuscode:200",
                "limit": "20",
            },
        )
    except httpx.HTTPError:
        return None
    if listing.status_code >= 400:
        return None
    try:
        rows = listing.json()
    except json.JSONDecodeError:
        return None
    snapshots = [row for row in rows[1:] if isinstance(row, list) and len(row) >= 2]
    if not snapshots:
        return None
    timestamp, original = snapshots[-1][0], snapshots[-1][1]
    raw_url = f"https://web.archive.org/web/{timestamp}id_/{original}"
    try:
        page = client.get(raw_url)
    except httpx.HTTPError:
        return None
    if page.status_code >= 400 or not page.text.strip():
        return None
    return page.text, raw_url


def fetch_archive_ph(client: httpx.Client, url: str) -> tuple[str, str] | None:
    target = archive_target(url)
    try:
        page = client.get(f"https://archive.ph/newest/{target}")
    except httpx.HTTPError:
        return None
    if page.status_code >= 400 or not page.text.strip():
        return None
    return page.text, str(page.url)


def _live(html: str | None, url: str, method: str) -> tuple[str, str, str] | None:
    if not html:
        return None
    return html, url, method


def _cached(found: tuple[str, str] | None, method: str) -> tuple[str, str, str] | None:
    if found is None:
        return None
    html, source_url = found
    return html, source_url, method


def _accept(
    cache_dir: Path,
    url: str,
    html: str,
    source_url: str,
    method: str,
) -> tuple[str, str, str | None, str] | None:
    if not html or not page_has_product(html, url):
        return None
    path = write_local_copy(cache_dir, url, html, method, source_url)
    local = path.read_text(encoding="utf-8")
    if not page_has_product(local, url):
        return None
    if method in CACHE_METHODS:
        note = cache_note(url, source_url)
    elif method == "exa" and _host(source_url) != _host(url):
        note = blocked_note(url, source_url)
    else:
        note = None
    return local, source_url, note, method


def load_exa_key() -> str | None:
    for path in (Path("api_config.json"), Path(__file__).resolve().parents[2] / "api_config.json"):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = payload.get("EXA_API_KEY") if isinstance(payload, dict) else None
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def fetch_exa(url: str) -> tuple[str, str, str] | None:
    """Live-crawl the product URL with Exa, then search if that page has no text."""
    key = load_exa_key()
    if not key:
        return None
    page = _exa_contents(key, archive_target(url))
    if page is None:
        page = _exa_search_page(key, url)
    if page is None:
        return None
    source = str(page.get("url") or url)
    return html_from_exa(page), source, "exa"


def html_from_exa(page: dict) -> str:
    """Turn Exa's text result into a local HTML page the product parser can read."""
    title = page.get("title") if isinstance(page.get("title"), str) else ""
    text = page.get("text") if isinstance(page.get("text"), str) else ""
    title = title.strip() or _first_line(text)
    paragraphs = "".join(f"<p>{html.escape(part.strip())}</p>" for part in text.split("\n") if part.strip())
    image = page.get("image") if isinstance(page.get("image"), str) else ""
    image_tag = ""
    if image.startswith("https://") and any(image.lower().split("?", 1)[0].endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif")):
        image_tag = f'<meta property="og:image" content="{html.escape(image, quote=True)}" />'
    return (
        "<html><head>"
        f"<title>{html.escape(title)}</title>"
        f'<meta property="og:title" content="{html.escape(title, quote=True)}" />'
        f"{image_tag}</head><body><h1>{html.escape(title)}</h1>{paragraphs}</body></html>"
    )


def _exa_contents(key: str, url: str) -> dict | None:
    payload = _exa_post(
        key,
        "https://api.exa.ai/contents",
        {"urls": [url], "text": True, "maxAgeHours": 0, "livecrawlTimeout": 20000},
    )
    return _first_exa_page(payload)


def _exa_search_page(key: str, url: str) -> dict | None:
    payload = _exa_post(
        key,
        "https://api.exa.ai/search",
        {
            "query": _exa_query(url),
            "numResults": 5,
            "contents": {"text": True},
        },
    )
    requested = _host(url)
    for page in _exa_pages(payload):
        if _host(str(page.get("url") or "")) == requested:
            continue
        return page
    return None


def _exa_post(key: str, endpoint: str, body: dict) -> dict | None:
    try:
        response = httpx.post(
            endpoint,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=45,
        )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _first_exa_page(payload: dict | None) -> dict | None:
    pages = _exa_pages(payload)
    return pages[0] if pages else None


def _exa_pages(payload: dict | None) -> list[dict]:
    if not payload:
        return []
    statuses = {
        item.get("id"): item.get("status")
        for item in payload.get("statuses") or []
        if isinstance(item, dict)
    }
    pages: list[dict] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        status = statuses.get(item.get("id")) or statuses.get(item.get("url"))
        if status not in (None, "success"):
            continue
        title = item.get("title") if isinstance(item.get("title"), str) else ""
        text = item.get("text") if isinstance(item.get("text"), str) else ""
        source = item.get("url") if isinstance(item.get("url"), str) else ""
        if not source.startswith("http") or shell_title(title) or document_is_blocked(text):
            continue
        if len(text.split()) < 5 and not title.strip():
            continue
        pages.append(item)
    return pages


def _exa_query(url: str) -> str:
    path = unquote(urlparse(archive_target(url)).path)
    words = []
    for part in path.split("/"):
        if not part or part.lower() in {"dp", "gp", "product", "products"} or part.lower().startswith("ref="):
            continue
        words.append(part.replace("-", " "))
    return " ".join([*words[:6], *product_clues(url)])[:300] or url


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:180]
    return "Product"


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")
