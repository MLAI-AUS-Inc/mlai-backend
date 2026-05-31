"""Access to the Watt The Hack simulation engine package.

The source of truth remains the Watt-The-Hack engine repository. In local
workspace development we can import it from the sibling repo; production should
install the public engine package from requirements.txt.
"""

from __future__ import annotations

import sys
from pathlib import Path

from django.conf import settings


def ensure_engine_importable() -> None:
    try:
        import watt_the_hack  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    workspace_engine = Path(settings.BASE_DIR).parent / "Watt-The-Hack"
    if workspace_engine.exists():
        sys.path.insert(0, str(workspace_engine))


ensure_engine_importable()

