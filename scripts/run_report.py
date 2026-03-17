"""Generate a reliability report and print it to stdout.

Usage:
    uv run python -m scripts.run_report
    # or via Makefile:
    make report
"""

import asyncio
import logging
import sys

from src.report.generator import generate_report

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s: %(message)s",
)


async def main() -> None:
    """Generate and print the report."""
    try:
        result = await generate_report()
        print(result.markdown)
    except Exception as e:
        print(f"Failed to generate report: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
