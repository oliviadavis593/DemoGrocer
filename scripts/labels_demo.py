"""Generate sample product labels and report the output paths."""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from packages.odoo_client import OdooClient, OdooClientError
from services.docs import MarkdownLabelGenerator


DEFAULT_CODES = ["FF101", "FF102"]


def main(args: list[str] | None = None) -> None:
    codes = args if args else DEFAULT_CODES
    client = OdooClient()
    try:
        client.authenticate()
    except OdooClientError as exc:
        raise SystemExit(f"Failed to authenticate with Odoo: {exc}") from exc
    output_dir = ROOT / "out" / "labels"
    generator = MarkdownLabelGenerator(client, output_dir=output_dir)
    documents = generator.generate(codes)
    print(f"Generating labels for {len(codes)} product codes")
    if not documents:
        print("No labels were generated; verify the requested default codes.")
        return
    print(f"Output directory: {output_dir}")
    for doc in documents:
        status = "found" if doc.found else "missing"
        print(f"- {doc.default_code}: {doc.pdf_path} ({status})")


if __name__ == "__main__":
    main(sys.argv[1:])
