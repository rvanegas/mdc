from __future__ import annotations

import bisect
from dataclasses import dataclass
import re


_H = r"#{2}"
HEADING_RE = re.compile(rf"^({_H})\s+(.+?)\s*$", re.MULTILINE)
_REFS_HEADING_RE = re.compile(rf"^({_H}) References\s*$", re.MULTILINE)
_NOTES_HEADING_RE = re.compile(rf"^({_H}) Notes\s*$", re.MULTILINE)
_RELATED_HEADING_RE = re.compile(rf"^({_H}) Related\s*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(rf"^{_H} ", re.MULTILINE)
ASSISTANT_NAME = "Claude"  # default for mdc-format transcripts
REFERENCE_LINE_RE = re.compile(r"^\| .+\([^)]+\).+$")
_REF_TITLE_SPLIT_RE = re.compile(r"(\([^)]+\)\s+)")


def _normalize_ref(ref: str) -> str:
    """Ensure the title portion of a reference line is italicized."""
    m = _REF_TITLE_SPLIT_RE.search(ref)
    if not m:
        return ref
    prefix = ref[: m.end()]
    title = ref[m.end():]
    if title.startswith("*"):
        return ref
    return f"{prefix}*{title}*"


def _normalize_related(line: str) -> str:
    """Ensure the title portion of a Related line ('| Title') is italicized."""
    if not line.startswith("| "):
        return line
    title = line[2:].strip()
    if title.startswith("*") and title.endswith("*"):
        return line
    return f"| *{title}*"


class TranscriptError(ValueError):
    """Raised when a transcript is malformed."""


@dataclass(frozen=True)
class Turn:
    speaker: str
    content: str
    is_assistant: bool
    heading: str = "##"


@dataclass(frozen=True)
class Preamble:
    title: str
    date: str    # "yyyy-mm-dd"
    raw: str


@dataclass(frozen=True)
class Transcript:
    source_text: str
    preamble: Preamble
    turns: tuple[Turn, ...]
    pending_turn: Turn | None
    references: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    related: tuple[str, ...] = ()

    @property
    def pending(self) -> bool:
        return self.pending_turn is not None


def _parse_preamble(text: str) -> Preamble:
    """Parse the mdc preamble from the text before the first ## heading.

    Expected structure (0-indexed lines):
      lines[0]: blank
      lines[1]: # Title
      lines[2]: yyyy-mm-dd
      lines[3]: blank

    Raises TranscriptError if the preamble is malformed or absent.
    """
    lines = text.split("\n")
    if len(lines) < 4:
        raise TranscriptError(
            "File must start with the mdc preamble: blank line, # Title, date (yyyy-mm-dd), blank line."
        )
    if lines[0].strip() != "":
        raise TranscriptError(
            f"Expected blank first line before preamble, got {lines[0]!r}."
        )
    if not re.match(r"^# .+", lines[1]):
        raise TranscriptError(
            f"Expected '# Title' on line 2, got {lines[1]!r}."
        )
    title = lines[1][2:].strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", lines[2]):
        raise TranscriptError(
            f"Expected date 'yyyy-mm-dd' on line 3, got {lines[2]!r}. "
            "Run 'mdc fix' to auto-correct common date formatting issues."
        )
    date_str = lines[2]
    if lines[3].strip() != "":
        raise TranscriptError(
            f"Expected blank line after date on line 4, got {lines[3]!r}."
        )
    return Preamble(title=title, date=date_str, raw=text)


def parse_transcript(text: str, assistant_name: str = ASSISTANT_NAME) -> Transcript:
    if text.lstrip().startswith("---"):
        raise TranscriptError("Transcript files cannot start with YAML frontmatter.")

    matches = list(HEADING_RE.finditer(text))
    if not matches:
        raise TranscriptError("Transcript must contain at least one '## <Name>' section heading.")

    # The mdc format requires a preamble before the first ## heading.
    preamble_text = text[:matches[0].start()]
    if not preamble_text.strip():
        raise TranscriptError(
            "File must start with the mdc preamble (blank line, # Title, date, blank line) "
            "before the first '## <Name>' section."
        )
    preamble = _parse_preamble(preamble_text)

    turns: list[Turn] = []
    references: list[str] = []
    notes: list[str] = []
    related: list[str] = []
    last_was_assistant = False
    seen_references = False

    for index, match in enumerate(matches):
        heading = match.group(1)
        speaker = match.group(2).strip()
        if not speaker:
            raise TranscriptError("Found an empty heading. Each turn needs a speaker name.")

        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[body_start:body_end]

        if speaker == "References":
            seen_references = True
            for line in content.splitlines():
                stripped = line.strip()
                if stripped and REFERENCE_LINE_RE.match(stripped):
                    references.append(_normalize_ref(stripped))
            continue

        if speaker == "Notes":
            if seen_references:
                raise TranscriptError("'## Notes' must come before '## References'.")
            for line in content.splitlines():
                stripped = line.strip()
                if stripped:
                    notes.append(stripped)
            continue

        if speaker == "Related":
            if seen_references:
                raise TranscriptError("'## Related' must come before '## References'.")
            for line in content.splitlines():
                stripped = line.strip()
                if stripped:
                    related.append(stripped)
            continue

        is_assistant = speaker == assistant_name
        if last_was_assistant and is_assistant:
            raise TranscriptError(f"Found two '## {assistant_name}' turns in a row. A human turn must come between assistant replies.")

        if not is_assistant and not content.strip():
            raise TranscriptError(f"Turn '{speaker}' is empty. Human turns need message content.")

        if is_assistant and not content.strip():
            raise TranscriptError(f"Turn '{assistant_name}' is empty. Assistant turns need message content.")

        turns.append(Turn(speaker=speaker, content=content, is_assistant=is_assistant, heading=heading))
        last_was_assistant = is_assistant

    pending_turn = turns[-1] if turns and not turns[-1].is_assistant else None
    return Transcript(
        source_text=text,
        preamble=preamble,
        turns=tuple(turns),
        pending_turn=pending_turn,
        references=tuple(references),
        notes=tuple(notes),
        related=tuple(related),
    )


def append_assistant_reply(text: str, reply: str, assistant_name: str = ASSISTANT_NAME, heading: str = "##") -> str:
    cleaned_reply = reply.strip()
    if not cleaned_reply:
        raise TranscriptError("Cannot append an empty assistant reply.")

    new_turn = f"{heading} {assistant_name}\n\n{cleaned_reply}"

    notes_match = _NOTES_HEADING_RE.search(text)
    related_match = _RELATED_HEADING_RE.search(text)
    refs_match = _REFS_HEADING_RE.search(text)

    # Insert before whichever special section (Notes, Related, or References) comes first
    candidates = [m for m in (notes_match, related_match, refs_match) if m is not None]
    anchor = min(candidates, key=lambda m: m.start()) if candidates else None

    if anchor:
        before = text[:anchor.start()].rstrip()
        tail = text[anchor.start():].rstrip()
        separator = "\n\n" if before else ""
        return f"{before}{separator}{new_turn}\n\n{tail}\n"

    base = text.rstrip()
    separator = "\n\n" if base else ""
    return f"{base}{separator}{new_turn}\n"


def extract_references(reply: str) -> tuple[str, list[str]]:
    """Scan lines from the end of reply, collecting trailing reference lines.

    Stops at the first non-blank, non-matching line. Returns (body, refs) where
    body is the reply with the trailing ref block stripped.
    """
    lines = reply.splitlines()
    refs: list[str] = []
    i = len(lines) - 1

    while i >= 0:
        line = lines[i]
        if not line.strip():
            i -= 1
            continue
        if REFERENCE_LINE_RE.match(line.strip()):
            refs.insert(0, _normalize_ref(line.strip()))
            i -= 1
        else:
            break

    # Strip a bare "References" label or markdown heading (any level)
    # that the model may emit before the reference list.
    if i >= 0 and re.sub(r"^#+\s*", "", lines[i].strip()).lower().rstrip(":") == "references":
        i -= 1

    body = "\n".join(lines[: i + 1]).rstrip()
    return body, refs


def _ref_sort_key(ref: str) -> tuple[str, int]:
    """Return (name_key, year_int) for sorting references."""
    paren_idx = ref.find(" (")
    if paren_idx == -1:
        name_part = ref
        parenthetical = ""
    else:
        name_part = ref[:paren_idx]
        parenthetical = ref[paren_idx:]

    name_key = re.sub(r"[\W_]+", "", name_part).lower()

    year_match = re.search(r"\d+", parenthetical)
    year = int(year_match.group()) if year_match else 0
    if re.search(r"BCE|BC", parenthetical, re.IGNORECASE):
        year = -year

    return (name_key, year)


def insert_references(existing: list[str], new_refs: list[str]) -> list[str]:
    """Insert new_refs into existing, deduplicating by exact match, maintaining sorted order."""
    result = list(existing)
    for ref in new_refs:
        if ref in result:
            continue
        keys = [_ref_sort_key(e) for e in result]
        key = _ref_sort_key(ref)
        idx = bisect.bisect_left(keys, key)
        result.insert(idx, ref)
    return result


def extract_related(reply: str) -> tuple[str, list[str]]:
    """Extract the ## Related section from a reply, returning (body, titles).

    Titles are raw lines such as '| *Title*'. The section is removed from body.
    """
    match = _RELATED_HEADING_RE.search(reply)
    if not match:
        return reply, []

    section_start = match.start()
    after_heading = reply[match.end():]
    next_heading = _NEXT_HEADING_RE.search(after_heading)
    if next_heading:
        content = after_heading[:next_heading.start()]
        tail = reply[match.end() + next_heading.start():]
    else:
        content = after_heading
        tail = ""

    titles = [_normalize_related(line.strip()) for line in content.splitlines() if line.strip().startswith("| ")]

    before = reply[:section_start].rstrip()
    if tail.strip():
        body = before + "\n\n" + tail.lstrip("\n")
    else:
        body = before

    return body, titles


def update_related_section(text: str, titles: list[str]) -> str:
    """Replace (or insert before ## References) the ## Related section."""
    related_heading = _RELATED_HEADING_RE.search(text)

    if related_heading:
        heading_marker = related_heading.group(1)
        section_start = related_heading.start()
        after_heading = text[related_heading.end():]
        next_heading = _NEXT_HEADING_RE.search(after_heading)
        if next_heading:
            section_end = related_heading.end() + next_heading.start()
        else:
            section_end = len(text)
        before = text[:section_start].rstrip()
        tail = text[section_end:]
        content = "\n".join(titles)
        rebuilt = f"{heading_marker} Related\n\n{content}"
        if tail.strip():
            return (before + "\n\n" if before else "") + rebuilt + "\n\n" + tail.lstrip("\n")
        return (before + "\n\n" if before else "") + rebuilt + "\n"

    heading_marker = "##"
    content = "\n".join(titles)
    refs_match = _REFS_HEADING_RE.search(text)
    if refs_match:
        before = text[:refs_match.start()].rstrip()
        tail = text[refs_match.start():]
        return f"{before}\n\n{heading_marker} Related\n\n{content}\n\n{tail}"
    base = text.rstrip()
    return f"{base}\n\n{heading_marker} Related\n\n{content}\n"


def update_references_section(text: str, refs: list[str]) -> str:
    """Replace (or append) the ## References section with the given refs list."""
    refs_heading = _REFS_HEADING_RE.search(text)

    if refs_heading:
        heading_marker = refs_heading.group(1)
        # Remove the existing section
        section_start = refs_heading.start()
        after_heading = text[refs_heading.end():]
        next_heading = _NEXT_HEADING_RE.search(after_heading)
        if next_heading:
            section_end = refs_heading.end() + next_heading.start()
        else:
            section_end = len(text)
        before = text[:section_start].rstrip()
        text = (before + "\n\n" if before else "") + text[section_end:]
    else:
        heading_marker = "##"

    base = text.rstrip()
    refs_content = "\n".join(refs)
    return f"{base}\n\n{heading_marker} References\n\n{refs_content}\n"
