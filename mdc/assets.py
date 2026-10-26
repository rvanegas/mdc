from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import urlparse

from mdc.transcript import Transcript, TranscriptError, Turn


LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)]+)\)")
MARKDOWN_SUFFIXES = {".md", ".markdown"}
TEXT_SUFFIXES = {".txt"}
RTF_SUFFIXES = {".rtf"}
IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
PDF_MEDIA_TYPE = "application/pdf"


@dataclass(frozen=True)
class LocalAssetReference:
    label: str
    raw_target: str
    path: Path
    kind: str


def collect_local_assets(transcript: Transcript, transcript_path: Path) -> dict[int, tuple[LocalAssetReference, ...]]:
    scope = _AssetScope.for_transcript(transcript_path)
    assets_by_turn: dict[int, tuple[LocalAssetReference, ...]] = {}

    for index, turn in enumerate(transcript.turns):
        assets = tuple(_collect_assets_for_turn(turn, scope))
        if assets:
            assets_by_turn[index] = assets

    # Collect assets linked from references and notes, attach to the pending (last human) turn.
    section_text = "\n".join(list(transcript.references) + list(transcript.notes))
    pending_index = next(
        (i for i, t in reversed(list(enumerate(transcript.turns))) if not t.is_assistant),
        None,
    )
    if section_text.strip() and pending_index is not None:
        existing = assets_by_turn.get(pending_index, ())
        seen_paths = {a.path for a in existing}
        section_assets = tuple(_collect_assets_from_text(section_text, scope, seen_paths))
        if section_assets:
            assets_by_turn[pending_index] = existing + section_assets

    return assets_by_turn


def _collect_assets_from_text(text: str, scope: "_AssetScope", seen_paths: set[Path] | None = None) -> list[LocalAssetReference]:
    if seen_paths is None:
        seen_paths = set()
    assets: list[LocalAssetReference] = []
    for match in LINK_RE.finditer(text):
        raw_target = _normalize_markdown_target(match.group(3))
        if not _is_local_target(raw_target):
            continue
        path = _resolve_asset_path(raw_target, scope)
        if path in seen_paths:
            continue
        assets.append(LocalAssetReference(
            label=match.group(2).strip(),
            raw_target=raw_target,
            path=path,
            kind=_classify_asset(path),
        ))
        seen_paths.add(path)
    return assets


def _collect_assets_for_turn(turn: Turn, scope: "_AssetScope") -> list[LocalAssetReference]:
    return _collect_assets_from_text(turn.content, scope)


def _normalize_markdown_target(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if " " in target:
        target = target.split(" ", 1)[0]
    return target


def _is_local_target(target: str) -> bool:
    if not target or target.startswith("#"):
        return False
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return False
    return True


class _AssetScope:
    """Where a transcript's links resolve: its library, else its own directory.

    Names are unique across a library, so a bare name is found wherever the
    library keeps it (its year directory, say), beside the transcript first.
    A link with a directory in it resolves from the transcript, as written.
    Either way the asset stays within the scope's root.
    """

    def __init__(self, transcript_dir: Path, root: Path) -> None:
        self.transcript_dir = transcript_dir
        self.root = root
        self._by_name: dict[str, list[Path]] | None = None

    @classmethod
    def for_transcript(cls, transcript_path: Path) -> "_AssetScope":
        from mdc.config import load_config
        transcript_dir = transcript_path.parent.resolve()
        library = load_config().library_path
        if library and _is_within_base_dir(transcript_dir, library.resolve()):
            return cls(transcript_dir, library.resolve())
        return cls(transcript_dir, transcript_dir)

    def find_by_name(self, name: str) -> list[Path]:
        if self._by_name is None:
            from mdc.config import LIBRARY_STATE_DIRNAME
            skip = {LIBRARY_STATE_DIRNAME, "REVISIONS"}
            self._by_name = {}
            for path in self.root.rglob("*"):
                if path.is_file() and not skip.intersection(path.relative_to(self.root).parts):
                    self._by_name.setdefault(path.name, []).append(path.resolve())
        return self._by_name.get(name, [])


def _resolve_asset_path(raw_target: str, scope: _AssetScope) -> Path:
    candidate = (scope.transcript_dir / raw_target).resolve(strict=False)
    if "/" not in raw_target and not candidate.exists():
        found = scope.find_by_name(raw_target)
        if len(found) > 1:
            listed = ", ".join(str(p.relative_to(scope.root)) for p in found)
            raise TranscriptError(f"Local asset '{raw_target}' is ambiguous: {listed}.")
        if found:
            candidate = found[0]
    if not _is_within_base_dir(candidate, scope.root):
        raise TranscriptError(
            f"Local asset '{raw_target}' must stay within '{scope.root}'."
        )
    if not candidate.exists():
        raise TranscriptError(f"Local asset '{raw_target}' does not exist.")
    if not candidate.is_file():
        raise TranscriptError(f"Local asset '{raw_target}' is not a file.")
    return candidate


def _is_within_base_dir(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _classify_asset(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in MARKDOWN_SUFFIXES:
        return "markdown"
    if suffix in TEXT_SUFFIXES:
        return "text"
    if suffix in RTF_SUFFIXES:
        return "rtf"
    if suffix in IMAGE_MEDIA_TYPES:
        return "image"
    if suffix == ".pdf":
        return "pdf"
    raise TranscriptError(
        f"Local asset '{path.name}' has unsupported type '{suffix or 'no extension'}'. "
        "Supported types are Markdown, text, RTF, PDF, and common web image formats."
    )


_CACHE_CONTROL: dict[str, object] = {"type": "ephemeral"}


def build_anthropic_input(
    transcript: Transcript,
    system_prompt: str,
    transcript_path: Path,
    library_context: str | None = None,
    resolve_file_id: Callable[[LocalAssetReference], str] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Build (system_blocks, messages) in Anthropic API format from a transcript."""
    assets_by_turn = collect_local_assets(transcript, transcript_path)

    system_blocks: list[dict[str, object]] = [
        {"type": "text", "text": system_prompt}
    ]
    if transcript.references:
        system_blocks.append({"type": "text", "text": "Accumulated references (do not repeat these in your reply — only return new references not already listed here):\n" + "\n".join(transcript.references)})
    if library_context:
        system_blocks.append({"type": "text", "text": library_context})
    system_blocks[-1]["cache_control"] = _CACHE_CONTROL

    # Anthropic allows at most 4 cache_control blocks per request; 1 is used by the system.
    cache_slots = 3

    messages: list[dict[str, object]] = []
    for index, turn in enumerate(transcript.turns):
        role = "assistant" if turn.is_assistant else "user"
        if turn.is_assistant:
            messages.append({"role": role, "content": turn.content.strip()})
            continue

        content_parts: list[dict[str, object]] = [
            {"type": "text", "text": f"{turn.speaker}:\n{turn.content.strip()}"}
        ]
        for asset in assets_by_turn.get(index, ()):
            file_id = resolve_file_id(asset) if resolve_file_id is not None else None
            use_cache = cache_slots > 0
            content_parts.extend(_build_anthropic_asset_parts(asset, cache=use_cache, file_id=file_id))
            if use_cache:
                cache_slots -= 1

        messages.append({"role": role, "content": content_parts})

    return system_blocks, messages


def _build_anthropic_asset_parts(asset: LocalAssetReference, cache: bool = True, file_id: str | None = None) -> list[dict[str, object]]:
    descriptor: dict[str, object] = {"type": "text", "text": f"Local attachment: {asset.raw_target}"}
    cc: dict[str, object] = {"cache_control": _CACHE_CONTROL} if cache else {}
    if asset.kind in ("markdown", "text", "rtf"):
        text = asset.path.read_text(encoding="utf-8")
        return [
            descriptor,
            {"type": "text", "text": f"--- Begin {asset.path.name} ---\n{text.strip()}\n--- End {asset.path.name} ---", **cc},
        ]
    if asset.kind == "image":
        if file_id is not None:
            return [descriptor, {"type": "image", "source": {"type": "file", "file_id": file_id}, **cc}]
        media_type = IMAGE_MEDIA_TYPES[asset.path.suffix.lower()]
        data = base64.b64encode(asset.path.read_bytes()).decode("ascii")
        return [
            descriptor,
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}, **cc},
        ]
    if asset.kind == "pdf":
        if file_id is not None:
            return [descriptor, {"type": "document", "source": {"type": "file", "file_id": file_id}, **cc}]
        data = base64.b64encode(asset.path.read_bytes()).decode("ascii")
        return [
            descriptor,
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}, **cc},
        ]
    raise AssertionError(f"Unexpected asset kind: {asset.kind}")

