"""Shared file-validation limits and errors without HTTP or storage dependencies."""

from dataclasses import dataclass

MAX_FILE_BYTES = 20_000_000


@dataclass(frozen=True)
class RowError:
    row: int
    field: str
    reason: str


class CatalogFileError(ValueError):
    def __init__(self, code: str, rows: list[RowError]):
        super().__init__("catalog file validation failed")
        self.code = code
        self.rows = rows
