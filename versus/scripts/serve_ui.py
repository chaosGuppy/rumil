"""Serve the human-judge UI."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from versus import ui  # noqa: E402


if __name__ == "__main__":
    ui.main()
