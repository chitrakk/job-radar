"""Check every configured ATS board token still returns live postings.

Board tokens rot: companies switch ATS, rename their board, or take it private. A dead
token fails silently inside the pipeline (by design — one dead board must not break the
run), so this script exists to surface them deliberately.

    uv run python scripts/verify_boards.py           # check all
    uv run python scripts/verify_boards.py --prune   # rewrite companies.yml without dead ones
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from jobradar.config import Config  # noqa: E402
from jobradar.fetcher import make_client  # noqa: E402
from jobradar.sources.ats import PROVIDERS  # noqa: E402


async def check(client, provider: str, entry) -> tuple[str, str, int, str]:
    token = entry["token"] if isinstance(entry, dict) else str(entry)
    name = entry.get("name", token) if isinstance(entry, dict) else token
    url = PROVIDERS[provider]["url"].format(token=token)
    try:
        resp = await client.get(url, timeout=25.0)
    except Exception as exc:  # noqa: BLE001
        return provider, name, 0, f"error: {type(exc).__name__}"
    if resp.status_code != 200:
        return provider, name, 0, f"http {resp.status_code}"
    try:
        jobs = PROVIDERS[provider]["parse"](token, name, resp.json())
    except Exception as exc:  # noqa: BLE001
        return provider, name, 0, f"parse: {type(exc).__name__}"
    return provider, name, len(jobs), "ok" if jobs else "empty"


async def main(prune: bool) -> None:
    cfg = Config()
    ats = cfg.companies.get("ats", {}) or {}

    tasks = []
    async with make_client() as client:
        for provider, entries in ats.items():
            if provider not in PROVIDERS:
                print(f"unknown provider in companies.yml: {provider}")
                continue
            tasks += [check(client, provider, e) for e in (entries or [])]
        results = await asyncio.gather(*tasks)

    live: dict[str, list] = {}
    print(f"{'provider':<16} {'company':<24} {'jobs':>5}  status")
    print("-" * 62)
    for provider, name, count, status in sorted(results, key=lambda r: (r[0], -r[2])):
        flag = "✓" if count else "✗"
        print(f"{flag} {provider:<14} {name:<24} {count:>5}  {status}")
        if count:
            live.setdefault(provider, []).append(name)

    total = sum(r[2] for r in results)
    ok = sum(1 for r in results if r[2])
    print("-" * 62)
    print(f"{ok}/{len(results)} boards live · {total} postings reachable")

    if prune:
        print("\n--prune: keep only the ✓ rows above in config/companies.yml")


if __name__ == "__main__":
    asyncio.run(main("--prune" in sys.argv))
