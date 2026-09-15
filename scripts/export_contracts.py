"""Export only implemented routes, plus separately labelled future input contracts."""

import json
from pathlib import Path

from evorec.api.app import create_app
from evorec.contracts import CatalogImportInput, FeedbackInput, RecommendationInput

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "contracts"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    documents = {
        "openapi.json": create_app().openapi(),
        "catalog-import.schema.json": CatalogImportInput.model_json_schema(),
        "feedback.schema.json": FeedbackInput.model_json_schema(),
        "recommendation.schema.json": RecommendationInput.model_json_schema(),
    }
    for name, value in documents.items():
        (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Exported {len(documents)} contract files to docs/contracts")


if __name__ == "__main__":
    main()
