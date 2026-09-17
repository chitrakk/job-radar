"""Config loading from the repo's config/ directory.

Kept as YAML rather than code so you can add a company board or a saved search by editing
one file and letting the scheduled workflow pick it up — no Python changes, no redeploy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import Query


def find_config_dir(start: Path | None = None) -> Path:
    """Walk up from the backend package to find config/ at the repo root."""
    here = (start or Path(__file__).resolve()).parent
    for candidate in [here, *here.parents]:
        cfg = candidate / "config"
        if cfg.is_dir() and (cfg / "sources.yml").exists():
            return cfg
    raise FileNotFoundError("could not locate config/ directory")


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


class Config:
    def __init__(self, config_dir: Path | None = None) -> None:
        self.dir = config_dir or find_config_dir()
        self.sources = _read(self.dir / "sources.yml")
        self.companies = _read(self.dir / "companies.yml")
        self.queries_raw = _read(self.dir / "queries.yml")

    def queries(self) -> list[Query]:
        defaults = self.queries_raw.get("defaults", {}) or {}
        out = []
        for entry in self.queries_raw.get("queries", []) or []:
            merged = {**defaults, **(entry or {})}
            merged.pop("name", None)
            out.append(Query(**merged))
        return out

    def source_options(self) -> dict[str, Any]:
        """Per-source config, with ATS company lists injected from companies.yml."""
        opts: dict[str, Any] = {"sources": dict(self.sources.get("sources", {}) or {})}
        for provider, companies in (self.companies.get("ats", {}) or {}).items():
            key = f"ats:{provider}"
            entry = dict(opts["sources"].get(key, {}) or {})
            entry["companies"] = companies or []
            opts["sources"][key] = entry
        return opts

    @property
    def window_days(self) -> int:
        return int(self.sources.get("corpus", {}).get("window_days", 60))
