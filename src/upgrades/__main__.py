"""CLI: ``python -m src.upgrades --dry-run`` runs the advisor without emailing or creating an approval batch."""

import argparse
import asyncio
import logging
import sys

from src.upgrades import email as upgrade_email
from src.upgrades.job import run_advisor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.upgrades")
    parser.add_argument("--dry-run", action="store_true", required=True, help="review and print; send nothing")
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    summary = asyncio.run(run_advisor(send_email=False))
    if summary.aborted:
        print(f"Upgrade advisor could not run: {summary.aborted} (see log)")  # noqa: T201
        return 1
    _, markdown, _ = upgrade_email.render(summary)
    print(markdown)  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
