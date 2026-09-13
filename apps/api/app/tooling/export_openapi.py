from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.main import create_app


def export_openapi(out_path: Path) -> None:
    app = create_app()
    schema = app.openapi()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the FastAPI OpenAPI document (D-251).")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    export_openapi(args.out)


if __name__ == "__main__":
    main()
