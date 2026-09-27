"""Read a URL list, crawl each pending URL, and record its output folder.

Text files use one URL per line. After a successful crawl the line becomes
``<url> <uuid>``. Lines that already include a UUID folder name are left
alone. JSON files are a list of ``{"url", "folder"}`` objects and are
updated the same way.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from product_crawler.crawl import CrawlerError

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class UrlFileError(Exception):
    pass


@dataclass
class Entry:
    kind: str
    url: str | None = None
    folder: str | None = None
    text: str = ""


def load(path: Path) -> tuple[str, list[Entry]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UrlFileError(f"could not read {path}: {exc}") from exc
    if path.suffix.lower() == ".json":
        return "json", _parse_json(raw, path)
    return "text", _parse_text(raw, path)


def save(path: Path, fmt: str, entries: list[Entry]) -> None:
    if fmt == "json":
        payload = [
            {"url": entry.url, "folder": entry.folder}
            for entry in entries
            if entry.kind == "job"
        ]
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    else:
        lines: list[str] = []
        for entry in entries:
            if entry.kind == "blank":
                lines.append("")
            elif entry.kind == "comment":
                lines.append(entry.text)
            elif entry.folder:
                lines.append(f"{entry.url} {entry.folder}")
            else:
                lines.append(entry.url or "")
        text = "\n".join(lines)
        if not text.endswith("\n"):
            text += "\n"
    path.write_text(text, encoding="utf-8")


def run_file(path: Path, crawl_fn: Callable[[str], Path]) -> int:
    fmt, entries = load(path)
    _validate(entries, path, fmt)
    failures = 0
    for entry in entries:
        if entry.kind != "job" or entry.folder:
            continue
        assert entry.url is not None
        try:
            out_dir = crawl_fn(entry.url)
        except CrawlerError as exc:
            print(f"error: {entry.url}: {exc}", file=sys.stderr)
            failures += 1
            continue
        entry.folder = Path(out_dir).name
        save(path, fmt, entries)
        try:
            from product_crawler.store import publish_output

            publish_output(Path(out_dir))
        except Exception as exc:
            print(
                f"error: saved {out_dir} but did not store it in the database: {exc}",
                file=sys.stderr,
            )
            failures += 1
            continue
        print(out_dir)
    return 1 if failures else 0


def _validate(entries: list[Entry], path: Path, fmt: str) -> None:
    label = "item" if fmt == "json" else "line"
    for index, entry in enumerate(entries, start=1):
        if entry.kind != "job" or entry.folder:
            continue
        if not _http_url(entry.url or ""):
            raise UrlFileError(
                f"{path} {label} {index}: URL must be an absolute http or https URL"
            )


def _http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _parse_text(raw: str, path: Path) -> list[Entry]:
    entries: list[Entry] = []
    for index, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            entries.append(Entry("blank"))
            continue
        if stripped.startswith("#"):
            entries.append(Entry("comment", text=stripped))
            continue
        parts = stripped.split()
        if len(parts) == 1:
            entries.append(Entry("job", url=parts[0]))
            continue
        if len(parts) == 2 and UUID_RE.match(parts[1]):
            entries.append(Entry("job", url=parts[0], folder=parts[1]))
            continue
        raise UrlFileError(
            f"{path} line {index}: expected '<url>' or '<url> <uuid-folder>'"
        )
    return entries


def _parse_json(raw: str, path: Path) -> list[Entry]:
    try:
        payload = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as exc:
        raise UrlFileError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise UrlFileError(f"{path} must be a JSON list of URLs")
    entries: list[Entry] = []
    for index, item in enumerate(payload, start=1):
        if isinstance(item, str):
            entries.append(Entry("job", url=item.strip()))
            continue
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise UrlFileError(
                f"{path} item {index}: expected a URL string or an object with 'url'"
            )
        folder = item.get("folder")
        if folder is None or folder == "":
            folder_name = None
        elif isinstance(folder, str) and UUID_RE.match(folder):
            folder_name = folder
        else:
            raise UrlFileError(
                f"{path} item {index}: 'folder' must be a UUID folder name or null"
            )
        entries.append(Entry("job", url=item["url"].strip(), folder=folder_name))
    return entries
