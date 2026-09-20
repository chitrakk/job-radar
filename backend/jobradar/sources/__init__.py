"""Source adapters.

`load_all()` imports every adapter module so their @register decorators fire. Tier 2
scrapers are imported defensively: they depend on Scrapling, which pulls in browser
binaries that are not installed in the light Tier 1 environment.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_loaded = False


def load_all() -> None:
    global _loaded
    if _loaded:
        return

    from . import (  # noqa: F401
        adzuna,
        ats,
        hn_hiring,
        indian_boards,
        linkedin_guest,
        naukri_sitemap,
        remote_feeds,
    )

    try:
        from . import scraped  # noqa: F401
    except ImportError as exc:
        # Expected whenever the `scrape` extra is not installed. Tier 1 is unaffected.
        log.info("tier 2 scrapers unavailable (%s); running tier 1 only", exc)

    _loaded = True


__all__ = ["load_all"]
