"""Local product pages.

Each scraped UUID is a page at http://127.0.0.1:8888/<uuid>. The page shows
one image, the product name, and the market barcode stored in DuckDB.
"""

from __future__ import annotations

import html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from product_crawler.jev import ask_jev, load_openrouter_key
from product_crawler.prompt_search import create_from_prompt
from product_crawler.store import (
    UUID_RE,
    ProductStore,
    default_db_path,
    default_output_root,
)

HOST = "127.0.0.1"
PORT = 8888


class ProductCreate(BaseModel):
    payload: dict[str, Any] | None = None
    output_id: str | None = None


class ProductUpdate(BaseModel):
    name: str | None = None
    brand: str | None = None
    barcode: str | None = Field(default=None)


class JevQuestion(BaseModel):
    ids: list[str]
    question: str


def create_app(
    db_path: Path | None = None,
    output_root: Path | None = None,
) -> FastAPI:
    store = ProductStore(db_path or default_db_path())
    root = output_root or default_output_root()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        store.init()
        store.import_missing(root)
        yield

    app = FastAPI(title="Product pages", lifespan=lifespan)
    app.state.store = store
    app.state.output_root = root

    @app.get("/", response_class=HTMLResponse)
    def index(search_error: str | None = None) -> str:
        products = store.list_products()
        return _page("Product Search Results : ", _index_html(products, search_error=search_error))

    @app.post("/search")
    async def search_prompt(request: Request) -> RedirectResponse:
        raw = (await request.body()).decode("utf-8", errors="replace")
        prompt = (parse_qs(raw).get("prompt") or [""])[0].strip()
        try:
            folder = create_from_prompt(prompt, root)
            store.import_folder(folder)
        except Exception:
            return RedirectResponse("/?search_error=1", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.post("/jev/decide")
    def jev_decide(body: JevQuestion) -> dict[str, Any]:
        question = body.question.strip()
        if not question:
            raise HTTPException(status_code=400, detail="Enter a question.")
        if len(question) > 2000:
            raise HTTPException(status_code=400, detail="Ask the question in 2000 characters or less.")
        if not body.ids:
            raise HTTPException(status_code=400, detail="Check at least one product.")
        api_key = load_openrouter_key()
        if not api_key:
            raise HTTPException(status_code=400, detail="OPENROUTER_API_KEY is not in api_config.json")
        products: list[dict[str, Any]] = []
        for product_id in body.ids:
            _require_uuid(product_id)
            row = store.get(product_id)
            if row is None:
                raise HTTPException(status_code=404, detail="product not found")
            products.append(row)
        try:
            return ask_jev(products, question, api_key)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/products")
    def list_products() -> list[dict[str, Any]]:
        return store.list_products()

    @app.get("/api/products/{product_id}")
    def read_product(product_id: str) -> dict[str, Any]:
        _require_uuid(product_id)
        row = store.get(product_id)
        if row is None:
            raise HTTPException(status_code=404, detail="product not found")
        return row

    @app.post("/api/products", status_code=201)
    def create_product(body: ProductCreate) -> dict[str, Any]:
        if body.output_id:
            _require_uuid(body.output_id)
            folder = root / body.output_id
            if not (folder / "product.json").is_file():
                raise HTTPException(status_code=404, detail="output folder not found")
            if store.get(body.output_id) is not None:
                raise HTTPException(status_code=409, detail="product already exists")
            return store.import_folder(folder)
        if not isinstance(body.payload, dict):
            raise HTTPException(status_code=400, detail="payload or output_id is required")
        try:
            return store.create(body.payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="product already exists") from exc

    @app.put("/api/products/{product_id}")
    def update_product(product_id: str, body: ProductUpdate) -> dict[str, Any]:
        _require_uuid(product_id)
        if body.name is None and body.brand is None and body.barcode is None:
            raise HTTPException(status_code=400, detail="name, brand, or barcode is required")
        updated = store.update(
            product_id,
            name=body.name,
            brand=body.brand,
            barcode=body.barcode,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="product not found")
        return updated

    @app.delete("/api/products/{product_id}", status_code=204)
    def delete_product(product_id: str) -> None:
        _require_uuid(product_id)
        if not store.delete(product_id):
            raise HTTPException(status_code=404, detail="product not found")

    @app.get("/{product_id}/image")
    def product_image(product_id: str) -> FileResponse:
        _require_uuid(product_id)
        row = store.get(product_id)
        if row is None or not row.get("image_file"):
            raise HTTPException(status_code=404, detail="image not found")
        image = _safe_image(root / product_id, row["image_file"])
        if image is None:
            raise HTTPException(status_code=404, detail="image not found")
        return FileResponse(image)

    @app.get("/{product_id}", response_class=HTMLResponse)
    def product_page(product_id: str) -> str:
        _require_uuid(product_id)
        row = store.get(product_id)
        if row is None:
            raise HTTPException(status_code=404, detail="product not found")
        return _page(row["name"] or "Product", _product_html(row))

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host=HOST, port=PORT)


def _index_html(products: list[dict[str, Any]], *, search_error: str | None) -> str:
    crawled = [item for item in products if item.get("origin") != "openai_prompt"]
    prompted = [item for item in products if item.get("origin") == "openai_prompt"]
    url_items = "\n".join(_result_item(item, arrow=False) for item in crawled)
    prompt_items = "\n".join(_result_item(item, arrow=True) for item in prompted)
    items = "\n".join(part for part in (url_items, prompt_items) if part) or "<li>No products stored yet.</li>"
    error = ""
    if search_error:
        error = '<p class="search-error">OpenAI search did not return a product. Try a more specific description.</p>'
    return (
        '<button type="button" id="page-reset">Reset</button>'
        "<h1>Product Search Results : </h1>"
        '<p class="index-note">Product search is performed by the built-in Crawler engine that scrapes the internet and then uses OpenAI APIs to complete the missing required data as well as additional searching on the product.</p>'
        '<p class="index-note index-note-arrow">An arrow (→) on a result means that page was generated from the text entered in the prompt. OpenAI API calls performed the search. The built-in Crawler was not used.</p>'
        f"<ul>{items}</ul>"
        '<section class="openai-section">'
        "<h1>Product Search by OpenAI API (Only) : </h1>"
        f"{error}"
        '<form class="index-form" method="post" action="/search">'
        '<label for="prompt">Describe the product</label>'
        '<textarea id="prompt" name="prompt" required maxlength="2000" placeholder="Describe the product you are searching for"></textarea>'
        '<button type="submit">Search</button>'
        "</form>"
        "</section>"
        '<section id="jev-section" class="openai-section">'
        "<h1>Decision Engine by Jev API (Only) : </h1>"
        '<p class="index-note">Check the products that are the options. Jev picks one of them for the question below, using each product page and the information stored for it.</p>'
        '<ul id="jev-selected"><li class="jev-empty">No products selected.</li></ul>'
        '<form id="jev-form" class="index-form">'
        '<label for="jev-question">Question</label>'
        '<textarea id="jev-question" maxlength="2000" placeholder="Question Jev answers by picking one checked product"></textarea>'
        '<button type="submit">Decide</button>'
        "</form>"
        '<div id="jev-decision" class="jev-decision"></div>'
        "</section>"
        "<script>"
        "const selected = new Map();"
        "const storageKey = 'jev-selected-ids';"
        "function storedIds() {"
        "  try { return JSON.parse(sessionStorage.getItem(storageKey) || '[]'); } catch (error) { return []; }"
        "}"
        "function remember() {"
        "  sessionStorage.setItem(storageKey, JSON.stringify([...selected.keys()]));"
        "}"
        "function render() {"
        "  const list = document.getElementById('jev-selected');"
        "  window.jevProducts = [...selected.values()];"
        "  list.replaceChildren();"
        "  if (selected.size === 0) {"
        "    const empty = document.createElement('li');"
        "    empty.className = 'jev-empty';"
        "    empty.textContent = 'No products selected.';"
        "    list.append(empty);"
        "    return;"
        "  }"
        "  for (const product of selected.values()) {"
        "    const item = document.createElement('li');"
        "    item.className = 'jev-card';"
        "    const link = document.createElement('a');"
        "    link.href = '/' + product.id;"
        "    link.textContent = product.name || product.id;"
        "    const detail = document.createElement('p');"
        "    detail.className = 'jev-detail';"
        "    const barcode = product.barcode ? ((product.barcode_type || 'Barcode') + ' ' + product.barcode) : 'No barcode';"
        "    const source = product.source_url || 'No source URL';"
        "    detail.textContent = [product.brand || 'No brand', barcode, source].join(' · ');"
        "    item.append(link, detail);"
        "    list.append(item);"
        "  }"
        "}"
        "async function useProduct(id, checked) {"
        "  if (!checked) { selected.delete(id); remember(); render(); return; }"
        "  const response = await fetch('/api/products/' + id);"
        "  if (!response.ok) return;"
        "  selected.set(id, await response.json());"
        "  remember();"
        "  render();"
        "}"
        "document.querySelectorAll('.jev-select').forEach((box) => {"
        "  box.addEventListener('change', () => { useProduct(box.value, box.checked); });"
        "  if (storedIds().includes(box.value)) { box.checked = true; useProduct(box.value, true); }"
        "});"
        "document.getElementById('page-reset').addEventListener('click', () => {"
        "  document.querySelectorAll('.jev-select').forEach((box) => { box.checked = false; });"
        "  selected.clear();"
        "  remember();"
        "  render();"
        "  document.getElementById('prompt').value = '';"
        "  document.getElementById('jev-question').value = '';"
        "  document.getElementById('jev-decision').replaceChildren();"
        "  const searchError = document.querySelector('.search-error');"
        "  if (searchError) searchError.remove();"
        "});"
        "function showDecision(message) {"
        "  const out = document.getElementById('jev-decision');"
        "  out.replaceChildren();"
        "  for (const line of String(message).split('\\n')) {"
        "    const row = document.createElement('p');"
        "    row.textContent = line;"
        "    out.append(row);"
        "  }"
        "}"
        "document.getElementById('jev-form').addEventListener('submit', async (event) => {"
        "  event.preventDefault();"
        "  const question = document.getElementById('jev-question').value.trim();"
        "  if (selected.size === 0) { showDecision('Check at least one product.'); return; }"
        "  if (!question) { showDecision('Enter a question.'); return; }"
        "  showDecision('Deciding…');"
        "  const response = await fetch('/jev/decide', {"
        "    method: 'POST',"
        "    headers: { 'Content-Type': 'application/json' },"
        "    body: JSON.stringify({ ids: [...selected.keys()], question }),"
        "  });"
        "  let payload = {};"
        "  try { payload = await response.json(); } catch (error) { payload = {}; }"
        "  if (!response.ok) {"
        "    showDecision(payload.detail || 'Jev did not return a decision.');"
        "    return;"
        "  }"
        "  showChoice(payload);"
        "});"
        "function choicePercent(value) {"
        "  return Math.round(Number(value) * 100) + '%';"
        "}"
        "function showChoice(payload) {"
        "  const out = document.getElementById('jev-decision');"
        "  out.replaceChildren();"
        "  const picked = document.createElement('p');"
        "  picked.textContent = 'Selected: ' + (payload.product_name || payload.choice);"
        "  const confidence = document.createElement('p');"
        "  confidence.textContent = 'Confidence: ' + (payload.confidence == null ? '' : choicePercent(payload.confidence));"
        "  const label = document.createElement('p');"
        "  label.textContent = 'Probability for each option';"
        "  const list = document.createElement('ul');"
        "  for (const option of payload.probabilities || []) {"
        "    const item = document.createElement('li');"
        "    item.textContent = option.name + ' — ' + choicePercent(option.probability);"
        "    list.append(item);"
        "  }"
        "  out.append(picked, confidence, label, list);"
        "}"
        "</script>"
    )


def _result_item(item: dict[str, Any], *, arrow: bool) -> str:
    product_id = html.escape(item["id"])
    name = html.escape(item["name"] or item["id"])
    marker = (
        ' <span class="openai-arrow" aria-hidden="true">→</span> '
        '<span class="openai-only">OpenAI API Only</span>'
        if arrow
        else ""
    )
    klass = ' class="openai-result"' if arrow else ""
    return (
        f"<li{klass}>"
        f'<input class="jev-select" type="checkbox" value="{product_id}" aria-label="Use {name} in Jev"> '
        f'<a href="/{product_id}">{name}</a>'
        f"{marker}"
        "</li>"
    )


def _require_uuid(product_id: str) -> None:
    if not UUID_RE.match(product_id):
        raise HTTPException(status_code=404, detail="product not found")


def _safe_image(folder: Path, relative: str) -> Path | None:
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        return None
    folder = folder.resolve()
    image = (folder / relative).resolve()
    if folder not in image.parents or not image.is_file():
        return None
    return image


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 40rem; color: #1c1c1c; }}
    #page-reset {{ margin: 0 0 1.25rem; font: inherit; padding: 0.35rem 0.8rem; }}
    a {{ color: #0b57d0; }}
    .index-note {{ margin: 0.35rem 0 0.35rem; font-size: 0.85rem; line-height: 1.4; color: #555; }}
    .index-note-arrow {{ margin-bottom: 1.25rem; }}
    .openai-section {{ margin-top: 2.5rem; padding-top: 1.25rem; border-top: 1px solid #ddd; }}
    .index-form textarea {{ width: 100%; min-height: 6rem; box-sizing: border-box; font: inherit; font-size: 0.95rem; padding: 0.5rem; }}
    .index-form button {{ margin-top: 0.6rem; font: inherit; padding: 0.35rem 0.8rem; }}
    .search-error {{ color: #8a1f1f; font-size: 0.9rem; }}
    .openai-result {{ margin: 0.35rem 0; }}
    .openai-arrow {{ margin-left: 0.45rem; }}
    .openai-only {{ margin-left: 0.35rem; color: #c40000; }}
    .jev-select {{ margin-right: 0.45rem; }}
    .jev-empty {{ color: #555; }}
    .jev-card {{ margin: 0.55rem 0 0.9rem; }}
    .jev-detail {{ margin: 0.15rem 0 0; color: #555; font-size: 0.85rem; word-break: break-all; }}
    .jev-decision {{ margin-top: 1rem; font-size: 1.05rem; }}
    .jev-decision p {{ margin: 0.2rem 0; }}
    img.product {{ width: 160px; height: 160px; object-fit: contain; background: #f3f3f3; }}
    .field-label {{ margin: 1rem 0 0; color: #555; }}
    .brand {{ font-size: 1.3rem; margin: 0.15rem 0 0; letter-spacing: 0.04em; }}
    .barcode-type {{ margin: 1rem 0 0; color: #555; }}
    .barcode {{ font-family: ui-monospace, monospace; font-size: 1.3rem; margin: 0.15rem 0 0.25rem; }}
    .barcode-note {{ margin: 0 0 1.5rem; color: #555; }}
    .barcode-openai {{ margin: 0.35rem 0 1.5rem; padding: 0.55rem 0.7rem; background: #fff3bf; border: 1px solid #e6c200; color: #5c4300; }}
    .source-url {{ word-break: break-all; margin: 0.15rem 0 0; }}
    .blocked-note {{ margin: 1rem 0 0; padding: 0.75rem; background: #fff6e8; border: 1px solid #e6d3b0; border-radius: 6px; }}
    .feedback {{ margin-top: 2rem; padding-top: 0.25rem; border-top: 1px solid #ddd; }}
    .feedback-list {{ max-height: 18rem; overflow-y: auto; margin-top: 0.5rem; padding: 0 0.85rem; border: 1px solid #ddd; border-radius: 6px; background: #fafafa; }}
    .feedback-item {{ padding: 0.85rem 0; border-bottom: 1px solid #e4e4e4; }}
    .feedback-item:last-child {{ border-bottom: 0; }}
    .feedback-meta {{ margin: 0; color: #555; font-size: 0.9rem; }}
    .feedback-text {{ margin: 0.35rem 0 0; white-space: pre-wrap; }}
    .videos {{ margin-top: 2rem; padding-top: 0.25rem; border-top: 1px solid #ddd; }}
    .video-list {{ margin: 0.5rem 0 0; padding: 0; list-style: none; }}
    .video-item {{ padding: 0.75rem 0; border-bottom: 1px solid #e4e4e4; }}
    .video-item:last-child {{ border-bottom: 0; }}
    .video-link {{ word-break: break-all; }}
    .research {{ margin-top: 2rem; padding-top: 0.25rem; border-top: 1px solid #ddd; }}
    .research-list {{ margin-top: 0.5rem; padding: 0 0.85rem; border: 1px solid #ddd; border-radius: 6px; background: #fafafa; }}
    .research-sources {{ margin: 0.35rem 0 0; padding-left: 1.1rem; }}
    .research-sources li {{ margin: 0.2rem 0; word-break: break-all; }}
    .research-item {{ padding: 0.75rem 0; border-bottom: 1px solid #e4e4e4; }}
    .research-item:last-child {{ border-bottom: 0; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def _product_html(row: dict[str, Any]) -> str:
    product_id = row["id"]
    name = row["name"] or "Untitled product"
    image = ""
    if row.get("image_file"):
        image = (
            f'<img class="product" alt="{html.escape(name)}" '
            f'src="/{html.escape(product_id)}/image">'
        )
    if row.get("brand"):
        brand_html = (
            '<p class="field-label">Brand</p>'
            f'<p class="brand" id="brand-value">{html.escape(row["brand"])}</p>'
        )
    else:
        brand_html = '<p class="brand" id="brand-value">No brand on this page</p>'
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    if row.get("barcode"):
        kind = row["barcode_type"] or "barcode"
        note = payload.get("barcode_note")
        notes = []
        if payload.get("barcode_from_openai"):
            notes.append(
                '<p class="barcode-openai" id="barcode-openai">Not found on this page. OpenAI found it.</p>'
            )
        if isinstance(note, str) and note.strip() and note.strip() != "Not found on this page. OpenAI found it.":
            notes.append(f'<p class="barcode-note">{html.escape(note.strip())}</p>')
        barcode_html = (
            f'<p class="barcode-type" id="barcode-type">{html.escape(kind)}</p>'
            f'<p class="barcode" id="barcode-value">{html.escape(row["barcode"])}</p>'
            f"{''.join(notes)}"
        )
    else:
        barcode_html = '<p class="barcode" id="barcode-value">No barcode on this page</p>'
    source = row.get("source_url") or payload.get("requested_url")
    if isinstance(source, str) and source.startswith(("http://", "https://")):
        source_html = (
            '<p class="field-label">Source</p>'
            f'<p class="source-url"><a id="source-url" href="{html.escape(source)}">'
            f"{html.escape(source)}</a></p>"
        )
    else:
        source_html = '<p class="source-url" id="source-url">No source URL stored</p>'
    note = payload.get("blocked_note")
    fetched = payload.get("fetched_url")
    if isinstance(note, str) and note.strip():
        fetched_html = ""
        if isinstance(fetched, str) and fetched.startswith(("http://", "https://")):
            fetched_html = (
                '<p class="source-url">Read from '
                f'<a id="fetched-url" href="{html.escape(fetched)}">{html.escape(fetched)}</a></p>'
            )
        source_html += (
            f'<p class="blocked-note" id="blocked-note">{html.escape(note.strip())}</p>'
            f"{fetched_html}"
        )
    return f"""
<p><a href="/">All products</a></p>
{image}
<h1 id="product-name">{html.escape(name)}</h1>
{brand_html}
{barcode_html}
{source_html}
{_feedback_section(payload)}
{_video_section(payload)}
{_research_section(payload)}
"""


def _feedback_section(payload: dict[str, Any]) -> str:
    reviews = payload.get("reviews") if isinstance(payload.get("reviews"), list) else []
    items: list[str] = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        comments = review.get("comments")
        if not isinstance(comments, str) or not comments.strip():
            continue
        meta = " · ".join(
            part
            for part in (
                review.get("nickname") if isinstance(review.get("nickname"), str) else None,
                _review_date(review.get("created_at")),
                _review_rating(review.get("rating")),
            )
            if part
        )
        headline = review.get("headline") if isinstance(review.get("headline"), str) else ""
        headline_html = f"<p><strong>{html.escape(headline.strip())}</strong></p>" if headline.strip() else ""
        meta_html = f'<p class="feedback-meta">{html.escape(meta)}</p>' if meta else ""
        items.append(
            '<article class="feedback-item">'
            f"{meta_html}{headline_html}"
            f'<p class="feedback-text">{html.escape(comments.strip())}</p>'
            "</article>"
        )
    if items:
        body = "".join(items)
    else:
        note = payload.get("reviews_note")
        message = note.strip() if isinstance(note, str) and note.strip() else "No user feedback found on this page."
        body = f'<p class="feedback-text">{html.escape(message)}</p>'
    return (
        '<section class="feedback" id="feedback" aria-label="User feedback">'
        "<h2>User feedback</h2>"
        f'<div class="feedback-list">{body}</div>'
        "</section>"
    )


def _video_section(payload: dict[str, Any]) -> str:
    videos = payload.get("videos")
    note = payload.get("videos_note")
    if not isinstance(videos, list) and not (isinstance(note, str) and note.strip()):
        return ""
    items: list[str] = []
    if isinstance(videos, list):
        for video in videos[:2]:
            if not isinstance(video, dict):
                continue
            url = video.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            title = video.get("title")
            label = title.strip() if isinstance(title, str) and title.strip() else url
            category = video.get("category")
            category_html = (
                f'<p class="feedback-meta">{html.escape(category.strip())}</p>'
                if isinstance(category, str) and category.strip()
                else ""
            )
            items.append(
                '<li class="video-item">'
                f"{category_html}"
                f'<a class="video-link" href="{html.escape(url)}">{html.escape(label)}</a>'
                "</li>"
            )
    if items:
        body = f'<ul class="video-list">{"".join(items)}</ul>'
    else:
        message = note.strip() if isinstance(note, str) and note.strip() else "No related video found."
        body = f'<p class="feedback-text">{html.escape(message)}</p>'
    return (
        '<section class="videos" id="videos" aria-label="Product videos">'
        "<h2>Videos</h2>"
        f"{body}"
        "</section>"
    )


_RESEARCH_LABELS = (
    ("audience_gender", "Who it is for"),
    ("popularity", "Popularity"),
    ("age_group", "Age group"),
    ("professionals", "Professionals"),
    ("demographic_appeal", "Demographic appeal"),
    ("top_us_state", "US state where it sells most"),
    ("top_us_city", "City in that state"),
    ("health_benefits", "Health benefits"),
    ("average_price", "Average market price"),
    ("bundle", "Often bought with"),
    ("recalls", "Recalls and public complaints"),
)


def _research_section(payload: dict[str, Any]) -> str:
    research = payload.get("research")
    if not isinstance(research, dict):
        return ""
    items = []
    for key, label in _RESEARCH_LABELS:
        answer = research.get(key)
        text = answer.strip() if isinstance(answer, str) and answer.strip() else "Unknown"
        items.append(
            '<article class="research-item">'
            f'<p class="feedback-meta">{html.escape(label)}</p>'
            f'<p class="feedback-text">{html.escape(text)}</p>'
            "</article>"
        )
    sources = research.get("sources")
    if isinstance(sources, list):
        links = []
        for url in sources:
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                links.append(f'<li><a href="{html.escape(url)}">{html.escape(url)}</a></li>')
        if links:
            items.append(
                '<article class="research-item">'
                '<p class="feedback-meta">Sources</p>'
                f'<ul class="research-sources">{"".join(links)}</ul>'
                "</article>"
            )
    return (
        '<section class="research" id="research" aria-label="Product research">'
        "<h2>Product research</h2>"
        f'<div class="research-list">{"".join(items)}</div>'
        "</section>"
    )


def _review_date(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:10]


def _review_rating(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return f"{value:g} stars"
    if isinstance(value, str) and value.strip():
        return f"{value.strip()} stars"
    return None


if __name__ == "__main__":
    main()
