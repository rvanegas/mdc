from __future__ import annotations

import re
import sys
import textwrap

_SPECIAL_LINE_RE = re.compile(
    r"^(?:#|[-*+] |\d+\. |\| |    |\t|[-*_]{3,}\s*$)"
)


def wrap_paragraphs(text: str, width: int = 100) -> str:
    """Wrap prose paragraphs at `width` columns; leave code fences, headings, lists, refs untouched."""
    lines = text.split("\n")
    result: list[str] = []
    in_code = False
    para: list[str] = []

    def flush() -> None:
        if para:
            if all(l.startswith("> ") for l in para):
                inner = " ".join(l[2:].rstrip() for l in para)
                wrapped = textwrap.fill(inner, width=max(1, width - 2), break_long_words=False, break_on_hyphens=False)
                result.extend("> " + l for l in wrapped.split("\n"))
            else:
                joined = " ".join(l.rstrip() for l in para)
                result.extend(textwrap.fill(joined, width=width, break_long_words=False, break_on_hyphens=False).split("\n"))
            para.clear()

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush()
            in_code = not in_code
            result.append(line)
            continue
        if in_code or not line.strip():
            flush()
            result.append(line)
            continue
        if _SPECIAL_LINE_RE.match(line):
            flush()
            result.append(line)
            continue
        para.append(line)

    flush()
    return "\n".join(result)


def _upgrade_reply_headings(text: str) -> str:
    """Promote any # or ## headings in the reply to ### to avoid colliding with turn delimiters.

    ## References and ## Related are exempt — they are structural section headings that must
    remain at ## to be recognized and merged by the transcript parser.
    """
    def promote(m: re.Match) -> str:
        hashes, rest = m.group(1), m.group(2)
        if rest.strip() in ("References", "Related"):
            return m.group(0)
        return "###" + rest

    return re.sub(r"^(#{1,2})(?!#)(.*)", promote, text, flags=re.MULTILINE)


def _parse_index_reply(text: str) -> tuple[str, list[str]]:
    summary_lines: list[str] = []
    terms_lines: list[str] = []
    mode = ""
    for line in text.splitlines():
        if line.startswith("SUMMARY:"):
            mode = "summary"
            rest = line[len("SUMMARY:"):].strip()
            if rest:
                summary_lines.append(rest)
        elif line.startswith("TERMS:"):
            mode = "terms"
            rest = line[len("TERMS:"):].strip()
            if rest:
                terms_lines.append(rest)
        elif mode == "summary" and line.strip():
            summary_lines.append(line.strip())
        elif mode == "terms" and line.strip():
            terms_lines.append(line.strip())
    summary = " ".join(summary_lines).strip()
    raw_terms = " ".join(terms_lines)
    terms = [t.strip() for t in raw_terms.split(";") if t.strip()]
    return summary, terms


def _parse_relate_reply(
    text: str, batch_ids: list[int], id_to_term: dict[int, str]
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    batch_id_set = set(batch_ids)
    for line in text.splitlines():
        if ":" not in line:
            continue
        left, _, right = line.partition(":")
        try:
            line_id = int(left.strip())
        except ValueError:
            continue
        if line_id not in batch_id_set:
            continue
        term = id_to_term[line_id]
        if right.strip().lower() == "none":
            result[term] = []
        else:
            related: list[str] = []
            for part in right.split(";"):
                try:
                    related_id = int(part.strip())
                except ValueError:
                    continue
                if related_id in id_to_term:
                    related.append(id_to_term[related_id])
            result[term] = related
    return result


def _print_reply_delta(chunk: str) -> None:
    sys.stdout.write(chunk)
    sys.stdout.flush()


def _lookup_price(model: str, table: dict[str, tuple[float, float]]) -> tuple[float, float] | None:
    for prefix, rates in table.items():
        if model.startswith(prefix):
            return rates
    return None


def _format_cost(dollars: float) -> str:
    return f"${dollars:.5f}"


def _format_total(dollars: float) -> str:
    return f"${dollars:.2f}"
