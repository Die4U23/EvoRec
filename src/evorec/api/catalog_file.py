"""Bounded, all-or-nothing CSV/JSON catalog validation."""

import csv
import io
import json
from uuid import UUID

from pydantic import ValidationError

from evorec.contracts import CatalogImportInput, CatalogItem
from evorec.domain.catalog_file import CatalogFileError, MAX_FILE_BYTES, RowError

MAX_ITEMS = 1000
FIELDS = {"item_id", "title", "category", "description", "image_url"}
REQUIRED = {"item_id", "title", "category"}


def parse_catalog_file(data: bytes, media_type: str, batch_id: UUID) -> CatalogImportInput:
    if len(data) > MAX_FILE_BYTES:
        raise CatalogFileError("file_too_large", [RowError(0, "file", "file exceeds 20 MB")])
    try:
        contents = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CatalogFileError("invalid_encoding", [RowError(0, "file", "expected UTF-8")]) from exc

    if media_type == "text/csv":
        try:
            reader = csv.DictReader(io.StringIO(contents, newline=""), strict=True)
            headers = reader.fieldnames
            if not headers or len(set(headers)) != len(headers) or not REQUIRED <= set(headers) or set(headers) - FIELDS:
                raise CatalogFileError("invalid_header", [RowError(1, "header", "expected unique item_id, title, category and optional description, image_url columns")])
            raw_rows = list(reader)
        except csv.Error as exc:
            raise CatalogFileError("invalid_csv", [RowError(0, "file", "malformed CSV")]) from exc
        first_row = 2
    elif media_type == "application/json":
        try:
            raw_rows = json.loads(contents)
        except json.JSONDecodeError as exc:
            raise CatalogFileError("invalid_json", [RowError(exc.lineno, "file", "malformed JSON")]) from exc
        if not isinstance(raw_rows, list):
            raise CatalogFileError("invalid_json", [RowError(0, "file", "expected an array of items")])
        first_row = 1
    else:
        raise CatalogFileError("unsupported_file_type", [RowError(0, "file", "expected text/csv or application/json")])

    if not 1 <= len(raw_rows) <= MAX_ITEMS:
        raise CatalogFileError("invalid_item_count", [RowError(0, "file", "expected 1 to 1000 items")])

    items = []
    errors = []
    seen = {}
    for index, raw in enumerate(raw_rows, first_row):
        if not isinstance(raw, dict) or None in raw:
            errors.append(RowError(index, "item", "expected one item with the declared columns"))
            continue
        if media_type == "text/csv" and raw.get("image_url") == "":
            raw["image_url"] = None
        try:
            item = CatalogItem.model_validate(raw)
        except ValidationError as exc:
            for problem in exc.errors():
                errors.append(RowError(index, ".".join(map(str, problem["loc"])), problem["msg"]))
            continue
        if item.item_id in seen:
            errors.append(RowError(index, "item_id", f"duplicate item_id; first at row {seen[item.item_id]}"))
        else:
            seen[item.item_id] = index
        items.append(item)
    if errors:
        raise CatalogFileError("invalid_catalog_items", errors)
    return CatalogImportInput(batch_id=batch_id, items=items)
