---
name: release
description: Bump the project version in pyproject.toml and build a wheel
---

Bump the version in `pyproject.toml` (field `version = "..."` under `[project]`) and then build a wheel.

1. Read `pyproject.toml` and show the user the current version.
2. Ask the user which part to bump — major, minor, or patch — unless they specified one in the args (e.g. `/release patch`). Default to patch if unspecified.
3. Compute the new version by incrementing the appropriate part and zeroing any lower parts.
4. Edit `pyproject.toml` with the new version string.
5. Run `python -m build --wheel` and show the output.
6. Report the new version and the path to the produced `.whl` file.
