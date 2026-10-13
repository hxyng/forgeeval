"""Write the OpenAPI contract to docs/openapi.json: `python -m forgeeval.api.openapi`.

The committed file is the contract the SDK and console build against;
tests/test_contract.py fails if it is stale.
"""

import json
from pathlib import Path

from forgeeval.api.main import app

CONTRACT_PATH = Path(__file__).resolve().parents[3] / "docs" / "openapi.json"


def render() -> str:
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    CONTRACT_PATH.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {CONTRACT_PATH}")
