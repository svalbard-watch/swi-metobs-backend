"""
Project information published on GitHub (contributors, data references, release),
served from our own origin so that visitors never contact GitHub.

The JSON is passed through unchanged. Each source can be pointed elsewhere (it must still
be a file on raw.githubusercontent.com) with SWI_PROJECT_INFO_<NAME>_URL.
"""

import hashlib
import json
import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response

from app.utils import github_proxy as gp

router = APIRouter()

RAW = f"https://{gp.RAW_HOST}/svalbard-watch"
DEFAULT_URLS = {
    "contributors": f"{RAW}/.github/refs/heads/main/contributors.json",
    "references": f"{RAW}/swi-production-deployment/refs/heads/main/info/references.json",
    "release": f"{RAW}/swi-production-deployment/refs/heads/main/info/version.json",
}


def source_url(name: str) -> str:
    return (
        os.getenv(f"SWI_PROJECT_INFO_{name.upper()}_URL", "").strip()
        or DEFAULT_URLS[name]
    )


def entry_name(name: str) -> str:
    return f"project:{name}"


async def refresh_project() -> None:
    """Fetch every project file. One failure stops the cycle: the backoff covers all of GitHub."""
    for name in DEFAULT_URLS:
        await gp.fetch(entry_name(name), source_url(name), as_json=True)


@router.get("/{name}")
async def get_project_info(
    name: Literal["contributors", "references", "release"], request: Request
):
    """A project file as published on GitHub: contributors, references or release."""
    entry = await gp.get_or_populate(entry_name(name), [refresh_project])
    if entry is None:
        raise HTTPException(
            status_code=503, detail="This information is not available yet"
        )

    body = json.dumps(entry["value"], separators=(",", ":"))
    etag = f'"{hashlib.sha256(body.encode()).hexdigest()[:16]}"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=3600"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
