"""mdc's HTTP API, run by `mdc serve` (SERVER.md).

Phase 1 answers only GET /healthz; Phase 2 adds files and runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI

# Written by bin/deploy into the package before the image is built, so it is
# baked in with the code it describes. Absent in a checkout, where the answer
# is "dev".
_DEPLOYED = Path(__file__).resolve().parent / "deployed.json"

app = FastAPI(title="mdc")


def _deployed() -> dict:
    try:
        return json.loads(_DEPLOYED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"commit": "dev"}


@app.get("/healthz")
def healthz() -> dict:
    """Unauthenticated: it says nothing but which commit is running."""
    return {"ok": True, **_deployed()}


def run_serve(host: str, port: int) -> int:
    import uvicorn
    uvicorn.run(app, host=host, port=port)
    return 0
