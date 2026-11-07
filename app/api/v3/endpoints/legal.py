"""
Privacy policy and terms of service, served from our own origin so that visitors never
contact GitHub.

SWI_LEGAL_REPO (`owner/repo`) names the repository that publishes the documents as
releases tagged `privacy-policy-vX.Y.Z` / `terms-of-service-vX.Y.Z`. The markdown is read
at the newest such tag, so only released text is served, never what is on a branch. When
the variable is unset, a placeholder shipped with the code is served instead: an instance
run by someone else never publishes another organisation's documents.
"""

import hashlib
import os
import re
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from loguru import logger
from pydantic import BaseModel

from app.utils import github_proxy as gp

router = APIRouter()

PLACEHOLDER_DIR = Path(__file__).resolve().parents[3] / "legal"
LANGUAGES = ("en",)
# document -> (release tag prefix, file name stem in the legal repository)
DOCUMENTS = {
    "privacy-policy": ("privacy-policy-v", "privacy_policy"),
    "terms-of-service": ("terms-of-service-v", "terms_of_service"),
}
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class LegalDocument(BaseModel):
    document: str
    language: str
    version: Optional[str] = None
    effective_date: Optional[str] = None
    source: Literal["release", "placeholder"]
    tag: Optional[str] = None
    content: str


def legal_repo() -> Optional[str]:
    repo = os.getenv("SWI_LEGAL_REPO", "").strip()
    if not repo:
        return None
    if not REPO_PATTERN.match(repo):
        logger.error("SWI_LEGAL_REPO must look like 'owner/repo', serving placeholders")
        return None
    return repo


def parse_front_matter(markdown: str) -> tuple[dict, str]:
    """Split a leading `---` YAML block of `key: value` lines from the body."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, markdown
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, markdown
    fields = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip().strip("\"'")
        fields[key.strip()] = None if value in ("", "null", "~") else value
    return fields, "\n".join(lines[end + 1 :]).lstrip("\n")


def pick_release_tag(releases: list, prefix: str) -> Optional[str]:
    """Newest published `<prefix>X.Y.Z` tag. Drafts and pre-releases are ignored."""
    best = None
    for release in releases:
        tag = release.get("tag_name") or ""
        if release.get("draft") or release.get("prerelease"):
            continue
        if not tag.startswith(prefix):
            continue
        match = SEMVER.match(tag[len(prefix) :])
        if match:
            version = tuple(int(part) for part in match.groups())
            if best is None or version > best[0]:
                best = (version, tag)
    return best[1] if best else None


def build_record(
    document: str, language: str, markdown: str, source: str, tag: Optional[str]
) -> dict:
    fields, body = parse_front_matter(markdown)
    return {
        "document": document,
        "language": language,
        "version": fields.get("version"),
        "effective_date": fields.get("effective_date"),
        "source": source,
        "tag": tag,
        "content": body,
    }


def placeholder(document: str, language: str) -> dict:
    stem = DOCUMENTS[document][1]
    path = PLACEHOLDER_DIR / f"{stem}_{language.upper()}.md"
    return build_record(
        document, language, path.read_text(encoding="utf-8"), "placeholder", None
    )


def entry_name(document: str, language: str) -> str:
    return f"legal:{document}:{language}"


async def refresh_legal() -> None:
    """Fetch the newest released text of every document (no-op without SWI_LEGAL_REPO)."""
    repo = legal_repo()
    if repo is None:
        return
    releases = await gp.fetch(
        "legal:releases",
        f"https://{gp.API_HOST}/repos/{repo}/releases?per_page=100",
        as_json=True,
    )
    for document, (prefix, stem) in DOCUMENTS.items():
        tag = pick_release_tag(releases["value"], prefix)
        if tag is None:
            logger.warning(f"No published release tagged {prefix}X.Y.Z in {repo}")
            continue
        for language in LANGUAGES:
            current = gp.get_entry(entry_name(document, language))
            # A tag never changes: the text is only fetched when a new one appears.
            if current and current["value"].get("tag") == tag:
                continue
            filename = f"{stem}_{language.upper()}.md"
            file = await gp.fetch(
                f"legal:file:{document}:{language}:{tag}",
                f"https://{gp.RAW_HOST}/{repo}/{tag}/{filename}",
            )
            gp.store(
                entry_name(document, language),
                build_record(document, language, file["value"], "release", tag),
            )


@router.get("/{document}", response_model=LegalDocument)
async def get_legal_document(
    document: Literal["privacy-policy", "terms-of-service"],
    request: Request,
    response: Response,
    lang: Literal["en"] = "en",
):
    """The current privacy policy or terms of service, as markdown with its version."""
    if legal_repo() is None:
        record = placeholder(document, lang)
    else:
        entry = await gp.get_or_populate(
            entry_name(document, lang), [refresh_legal], wait=10
        )
        if entry is None:
            raise HTTPException(
                status_code=503, detail="The legal document is not available yet"
            )
        record = entry["value"]

    digest = hashlib.sha256(
        f"{record['tag']}|{record['version']}|{record['content']}".encode()
    ).hexdigest()
    etag = f'"{digest[:16]}"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=3600"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return record
