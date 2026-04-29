"""Bundled prompt files.

Living inside the `rumil` package means they're shipped in the wheel and
available in non-editable installs (e.g. inside the API Docker image),
not just in the editable dev tree. Other modules locate prompt files via
the exported `PROMPTS_DIR`.
"""

from pathlib import Path

PROMPTS_DIR: Path = Path(__file__).resolve().parent

__all__ = ["PROMPTS_DIR"]
