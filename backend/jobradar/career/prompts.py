"""Load prompts from shared/prompts.json.

The same prompts drive the Python CLI and the browser app. Keeping them in one data file
rather than duplicating them in two languages is what stops a CV scored on the website
drifting away from the same CV scored at the terminal.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from .slop import shared_dir


@lru_cache(maxsize=1)
def prompts() -> dict[str, Any]:
    return json.loads((shared_dir() / "prompts.json").read_text())


def rules() -> dict[str, str]:
    return prompts()["shared_rules"]


def build(section: str, key: str = "template", **fields: Any) -> str:
    """Render a prompt template, injecting the shared grounding and voice rules.

    Templates use {braced} placeholders. `grounding` and `voice` are always available
    without the caller passing them, since every prompt needs at least one of them.
    """
    template: str = prompts()[section][key]
    return template.format(grounding=rules()["grounding"], voice=rules()["voice"], **fields)
