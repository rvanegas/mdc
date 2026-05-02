"""Sublime Text plugin for MDC conversation transcript files."""
from __future__ import annotations

import os
import re
import subprocess
import threading

import sublime
import sublime_plugin

_MDC_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-.+\.md$")
_SECTION_RE = re.compile(r"^## .+", re.MULTILINE)
_MDC_SYNTAX = "Packages/mdc/mdc.sublime-syntax"
_SETTINGS_FILE = "mdc.sublime-settings"


def _settings() -> sublime.Settings:
    return sublime.load_settings(_SETTINGS_FILE)


def _executable() -> str:
    return _settings().get("executable", "mdc")


def _is_mdc(view: sublime.View) -> bool:
    filename = view.file_name()
    return bool(filename and _MDC_FILENAME_RE.match(os.path.basename(filename)))


def _output_panel(window: sublime.Window) -> sublime.View:
    panel = window.find_output_panel("mdc")
    if panel is None:
        panel = window.create_output_panel("mdc")
        panel.settings().set("word_wrap", True)
        panel.settings().set("scroll_past_end", False)
    return panel


def _show_panel(window: sublime.Window) -> sublime.View:
    panel = _output_panel(window)
    window.run_command("show_panel", {"panel": "output.mdc"})
    return panel


def _append(panel: sublime.View, text: str) -> None:
    panel.run_command("append", {"characters": text, "force": True, "scroll_to_end": True})


def _run_async(window: sublime.Window, view: sublime.View, args: list[str],
               on_success: "callable[[], None] | None" = None) -> None:
    """Run mdc with ARGS in the file's directory, streaming into the output panel."""
    filepath = view.file_name()
    if not filepath:
        sublime.error_message("Buffer is not saved to a file.")
        return
    view.run_command("save")
    cwd = os.path.dirname(filepath)
    basename = os.path.basename(filepath)
    panel = _show_panel(window)
    _append(panel, f"$ mdc {' '.join(args)} {basename}\n")

    def worker() -> None:
        try:
            proc = subprocess.Popen(
                [_executable()] + args + [basename],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in proc.stdout:
                sublime.set_timeout(lambda l=line: _append(panel, l), 0)
            proc.wait()
            if proc.returncode == 0 and on_success:
                sublime.set_timeout(on_success, 100)
            elif proc.returncode != 0:
                sublime.set_timeout(
                    lambda: _append(panel, f"\n[exit {proc.returncode}]\n"), 0
                )
        except FileNotFoundError:
            sublime.set_timeout(
                lambda: _append(panel, f"Error: '{_executable()}' not found in PATH.\n"), 0
            )

    threading.Thread(target=worker, daemon=True).start()


# ── Commands ──────────────────────────────────────────────────────────────────

class MdcReplyCommand(sublime_plugin.WindowCommand):
    """Run `mdc reply` on the active file."""

    def run(self) -> None:
        view = self.window.active_view()
        _run_async(self.window, view, ["reply"],
                   on_success=lambda: view.run_command("revert"))

    def is_enabled(self) -> bool:
        view = self.window.active_view()
        return view is not None and bool(view.file_name())


class MdcFixCommand(sublime_plugin.WindowCommand):
    """Run `mdc fix` on the active file."""

    def run(self) -> None:
        view = self.window.active_view()
        _run_async(self.window, view, ["fix"],
                   on_success=lambda: view.run_command("revert"))

    def is_enabled(self) -> bool:
        view = self.window.active_view()
        return view is not None and bool(view.file_name())


class MdcCheckCommand(sublime_plugin.WindowCommand):
    """Run `mdc check` on the active file."""

    def run(self) -> None:
        view = self.window.active_view()
        _run_async(self.window, view, ["check"])

    def is_enabled(self) -> bool:
        view = self.window.active_view()
        return view is not None and bool(view.file_name())


class MdcValidateCommand(sublime_plugin.WindowCommand):
    """Run `mdc validate` on the active file."""

    def run(self) -> None:
        view = self.window.active_view()
        _run_async(self.window, view, ["validate"])

    def is_enabled(self) -> bool:
        view = self.window.active_view()
        return view is not None and bool(view.file_name())


class MdcNewCommand(sublime_plugin.WindowCommand):
    """Create a new MDC transcript."""

    def run(self) -> None:
        self.window.show_input_panel(
            "Transcript title:", "", self._on_done, None, None
        )

    def _on_done(self, title: str) -> None:
        title = title.strip()
        if not title:
            return
        view = self.window.active_view()
        cwd = (
            os.path.dirname(view.file_name())
            if view and view.file_name()
            else os.getcwd()
        )
        try:
            result = subprocess.run(
                [_executable(), "new", title],
                cwd=cwd,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            sublime.error_message(f"Error: '{_executable()}' not found in PATH.")
            return
        if result.returncode != 0:
            sublime.error_message(result.stdout.strip() or result.stderr.strip())
            return
        for filename in result.stdout.splitlines():
            filename = filename.strip()
            if filename.endswith(".md"):
                self.window.open_file(os.path.join(cwd, filename))


class MdcNextTurnCommand(sublime_plugin.TextCommand):
    """Move cursor to the next `## Speaker` heading."""

    def run(self, edit: sublime.Edit) -> None:
        point = self.view.sel()[0].end() if self.view.sel() else 0
        region = self.view.find(r"^## .+", point + 1)
        if region and region.begin() > point:
            self.view.sel().clear()
            self.view.sel().add(region.begin())
            self.view.show(region.begin())
        else:
            sublime.status_message("No more turns")


class MdcPrevTurnCommand(sublime_plugin.TextCommand):
    """Move cursor to the previous `## Speaker` heading."""

    def run(self, edit: sublime.Edit) -> None:
        point = self.view.sel()[0].begin() if self.view.sel() else 0
        # Search backwards by scanning all matches up to current point.
        all_regions = self.view.find_all(r"^## .+")
        before = [r for r in all_regions if r.begin() < point]
        if before:
            target = before[-1].begin()
            self.view.sel().clear()
            self.view.sel().add(target)
            self.view.show(target)
        else:
            sublime.status_message("No previous turn")


# ── Auto-detection ────────────────────────────────────────────────────────────

class MdcEventListener(sublime_plugin.EventListener):
    """Auto-assign MDC syntax when a .md file has an MDC preamble."""

    def on_load(self, view: sublime.View) -> None:
        if not _settings().get("auto_detect_syntax", True):
            return
        if view.syntax() and "mdc" in (view.syntax().path or "").lower():
            return
        if _is_mdc(view):
            view.assign_syntax(_MDC_SYNTAX)
