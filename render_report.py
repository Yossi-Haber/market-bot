"""Command-line entry point for deterministic Hebrew daily report rendering."""

from __future__ import annotations

import logging

from render.markdown import render_from_file


def main() -> None:
    """Render the existing composed snapshot without making network calls."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s render - %(message)s")
    render_from_file()


if __name__ == "__main__":
    main()
