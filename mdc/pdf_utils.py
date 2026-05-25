from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mdc.review import sanitize_for_pandoc

_TOC_BLOCK = """\
```{=latex}
\\tableofcontents
\\newpage
```

"""


def _prepend_toc(out_path: Path) -> None:
    content = out_path.read_text(encoding="utf-8")
    if not content.startswith(_TOC_BLOCK):
        out_path.write_text(_TOC_BLOCK + content, encoding="utf-8")


def _render_review_pdfs(*md_paths: Path) -> None:
    for md_path in md_paths:
        pdf_path = md_path.with_suffix(".pdf")
        for engine in ("xelatex", None):
            cmd = ["pandoc", str(md_path), "-o", str(pdf_path)]
            if engine:
                cmd += [f"--pdf-engine={engine}"]
            try:
                subprocess.run(cmd, check=True, capture_output=True)
                print(f"Wrote {pdf_path.name}.")
                break
            except FileNotFoundError:
                print("pandoc not found; skipping PDF generation.")
                return
            except subprocess.CalledProcessError as e:
                if engine is None:
                    print(f"pandoc error on {md_path.name}: {e.stderr.decode()[:300]}")
