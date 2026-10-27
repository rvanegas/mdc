from mdc.edit_tools import _write_version


def _versions(rev):
    return [p.read_text() for p in sorted(rev.iterdir(), key=lambda p: int(p.name.split("--")[1].split(".")[0]))]


def test_the_original_is_kept_before_the_first_write(tmp_path):
    doc, rev = tmp_path / "x.document.md", tmp_path / "REVISIONS"
    doc.write_text("original")
    _write_version(doc, "edited", revisions_dir=rev)
    assert _versions(rev) == ["original", "edited"]
    assert doc.read_text() == "edited"


def test_hand_edits_between_writes_are_kept(tmp_path):
    doc, rev = tmp_path / "x.document.md", tmp_path / "REVISIONS"
    doc.write_text("original")
    _write_version(doc, "first", revisions_dir=rev)
    _write_version(doc, "second", revisions_dir=rev)
    doc.write_text("by hand")
    _write_version(doc, "third", revisions_dir=rev)
    assert _versions(rev) == ["original", "first", "second", "by hand", "third"]


def test_a_new_file_starts_its_history(tmp_path):
    doc, rev = tmp_path / "x.document.md", tmp_path / "REVISIONS"
    _write_version(doc, "fresh", revisions_dir=rev)
    assert _versions(rev) == ["fresh"]
