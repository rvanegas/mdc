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
    base_dir = transcript_path.parent.resolve()
    assets_by_turn: dict[int, tuple[LocalAssetReference, ...]] = {}

    for index, turn in enumerate(transcript.turns):
        assets = tuple(_collect_assets_for_turn(turn, base_dir))
        if assets:
            assets_by_turn[index] = assets

    return assets_by_turn


def build_response_input(
    transcript: Transcript,
    system_prompt: str,
    transcript_path: Path,
    resolve_file_id: Callable[[LocalAssetReference], str] | None = None,
) -> list[dict[str, object]]:
    assets_by_turn = collect_local_assets(transcript, transcript_path)
    messages: list[dict[str, object]] = [{"role": "system", "content": system_prompt}]
    if transcript.references:
        messages.append({"role": "system", "content": "Accumulated references:\n" + "\n".join(transcript.references)})

    for index, turn in enumerate(transcript.turns):
        role = "assistant" if turn.is_assistant else "user"
        if turn.is_assistant:
            messages.append(
                {
                    "role": role,
                    "content": turn.content.strip(),
                }
            )
            continue

        content_parts: list[dict[str, object]] = [
            {
                "type": "input_text",
                "text": f"{turn.speaker}:\n{turn.content.strip()}",
            }
        ]
        for asset in assets_by_turn.get(index, ()):
            content_parts.extend(_build_asset_parts(asset, resolve_file_id=resolve_file_id))

        messages.append(
            {
                "type": "message",
                "role": role,
                "content": content_parts,
            }
        )

    if len(transcript.turns) > 1:
        messages.append({"role": "system", "content": system_prompt})

    return messages


def _collect_assets_for_turn(turn: Turn, base_dir: Path) -> list[LocalAssetReference]:
    seen_paths: set[Path] = set()
    assets: list[LocalAssetReference] = []
    for match in LINK_RE.finditer(turn.content):
        raw_target = _normalize_markdown_target(match.group(3))
        if not _is_local_target(raw_target):
            continue

        path = _resolve_asset_path(raw_target, base_dir)
        if path in seen_paths:
            continue

        assets.append(
            LocalAssetReference(
                label=match.group(2).strip(),
                raw_target=raw_target,
                path=path,
                kind=_classify_asset(path),
            )
        )
        seen_paths.add(path)
    return assets


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


def _resolve_asset_path(raw_target: str, base_dir: Path) -> Path:
    candidate = (base_dir / raw_target).resolve(strict=False)
    if not _is_within_base_dir(candidate, base_dir):
        raise TranscriptError(
            f"Local asset '{raw_target}' must stay within the transcript directory '{base_dir}'."
        )
    if not candidate.exists():
        raise TranscriptError(f"Local asset '{raw_target}' does not exist.")
    if not candidate.is_file():
        raise TranscriptError(f"Local asset '{raw_target}' is not a file.")
    return candidate


def _is_within_base_dir(path: Path, base_dir: Path) -> bool:
    try:
        path.relative_to(base_dir)
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
    library_manifest: str | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Build (system_blocks, messages) in Anthropic API format from a transcript."""
    assets_by_turn = collect_local_assets(transcript, transcript_path)

    system_blocks: list[dict[str, object]] = [
        {"type": "text", "text": system_prompt}
    ]
    if transcript.references:
        system_blocks.append({"type": "text", "text": "Accumulated references:\n" + "\n".join(transcript.references)})
    if library_manifest:
        system_blocks.append({"type": "text", "text": library_manifest})
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
            use_cache = cache_slots > 0
            content_parts.extend(_build_anthropic_asset_parts(asset, cache=use_cache))
            if use_cache:
                cache_slots -= 1

        messages.append({"role": role, "content": content_parts})

    return system_blocks, messages


def build_chat_input(
    transcript: Transcript,
    system_prompt: str,
    transcript_path: Path,
) -> list[dict[str, object]]:
    """Build messages in OpenAI Chat Completions format (for Ollama / compatible endpoints)."""
    assets_by_turn = collect_local_assets(transcript, transcript_path)
    messages: list[dict[str, object]] = [{"role": "system", "content": system_prompt}]
    if transcript.references:
        messages.append({"role": "system", "content": "Accumulated references:\n" + "\n".join(transcript.references)})

    for index, turn in enumerate(transcript.turns):
        role = "assistant" if turn.is_assistant else "user"
        if turn.is_assistant:
            messages.append({"role": role, "content": turn.content.strip()})
            continue

        assets = assets_by_turn.get(index, ())
        if not assets:
            messages.append({"role": role, "content": f"{turn.speaker}:\n{turn.content.strip()}"})
        else:
            content_parts: list[dict[str, object]] = [
                {"type": "text", "text": f"{turn.speaker}:\n{turn.content.strip()}"}
            ]
            for asset in assets:
                content_parts.extend(_build_chat_asset_parts(asset))
            messages.append({"role": role, "content": content_parts})

    return messages


def _build_chat_asset_parts(asset: LocalAssetReference) -> list[dict[str, object]]:
    descriptor: dict[str, object] = {"type": "text", "text": f"Local attachment: {asset.raw_target}"}
    if asset.kind in ("markdown", "text", "rtf"):
        text = asset.path.read_text(encoding="utf-8")
        return [
            descriptor,
            {"type": "text", "text": f"--- Begin {asset.path.name} ---\n{text.strip()}\n--- End {asset.path.name} ---"},
        ]
    if asset.kind == "image":
        media_type = IMAGE_MEDIA_TYPES[asset.path.suffix.lower()]
        return [
            descriptor,
            {"type": "image_url", "image_url": {"url": _to_data_url(media_type, asset.path.read_bytes())}},
        ]
    if asset.kind == "pdf":
        raise TranscriptError(
            f"PDF asset '{asset.raw_target}' is not supported for Ollama/chat-completions models."
        )
    raise AssertionError(f"Unexpected asset kind: {asset.kind}")


def _build_anthropic_asset_parts(asset: LocalAssetReference, cache: bool = True) -> list[dict[str, object]]:
    descriptor: dict[str, object] = {"type": "text", "text": f"Local attachment: {asset.raw_target}"}
    cc: dict[str, object] = {"cache_control": _CACHE_CONTROL} if cache else {}
    if asset.kind in ("markdown", "text", "rtf"):
        text = asset.path.read_text(encoding="utf-8")
        return [
            descriptor,
            {"type": "text", "text": f"--- Begin {asset.path.name} ---\n{text.strip()}\n--- End {asset.path.name} ---", **cc},
        ]
    if asset.kind == "image":
        media_type = IMAGE_MEDIA_TYPES[asset.path.suffix.lower()]
        data = base64.b64encode(asset.path.read_bytes()).decode("ascii")
        return [
            descriptor,
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}, **cc},
        ]
    if asset.kind == "pdf":
        data = base64.b64encode(asset.path.read_bytes()).decode("ascii")
        return [
            descriptor,
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}, **cc},
        ]
    raise AssertionError(f"Unexpected asset kind: {asset.kind}")


def _build_asset_parts(
    asset: LocalAssetReference,
    resolve_file_id: Callable[[LocalAssetReference], str] | None = None,
) -> list[dict[str, object]]:
    descriptor = {
        "type": "input_text",
        "text": f"Local attachment: {asset.raw_target}",
    }
    file_id = resolve_file_id(asset) if resolve_file_id is not None else None
    if asset.kind == "markdown":
        if file_id is not None:
            return [descriptor, {"type": "input_file", "file_id": file_id}]
        text = asset.path.read_text(encoding="utf-8")
        return [descriptor, {"type": "input_text", "text": f"--- Begin {asset.path.name} ---\n{text.strip()}\n--- End {asset.path.name} ---"}]
    if asset.kind == "text":
        if file_id is not None:
            return [descriptor, {"type": "input_file", "file_id": file_id}]
        text = asset.path.read_text(encoding="utf-8")
        return [descriptor, {"type": "input_text", "text": f"--- Begin {asset.path.name} ---\n{text.strip()}\n--- End {asset.path.name} ---"}]
    if asset.kind == "rtf":
        if file_id is not None:
            return [descriptor, {"type": "input_file", "file_id": file_id}]
        text = asset.path.read_text(encoding="utf-8")
        return [descriptor, {"type": "input_text", "text": f"--- Begin {asset.path.name} ---\n{text.strip()}\n--- End {asset.path.name} ---"}]
    if asset.kind == "image":
        if file_id is not None:
            return [descriptor, {"type": "input_image", "detail": "auto", "file_id": file_id}]
        media_type = IMAGE_MEDIA_TYPES[asset.path.suffix.lower()]
        return [descriptor, {"type": "input_image", "detail": "auto", "image_url": _to_data_url(media_type, asset.path.read_bytes())}]
    if asset.kind == "pdf":
        if file_id is not None:
            return [descriptor, {"type": "input_file", "file_id": file_id}]
        return [descriptor, {"type": "input_file", "filename": asset.path.name, "file_data": _to_data_url(PDF_MEDIA_TYPE, asset.path.read_bytes())}]
    raise AssertionError(f"Unexpected asset kind: {asset.kind}")


def _to_data_url(media_type: str, payload: bytes) -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{media_type};base64,{encoded}"
