from pathlib import Path
import zipfile

import pytest

from core.evidence_bundle import build_evidence_zip, collect_evidence_files, extract_evidence_paths


def test_extract_paths():
    paths = extract_evidence_paths(["见 reports/a/b.png 和 reports/x.webm", "reports/a/b.png"])
    assert paths == ["reports/a/b.png", "reports/x.webm"]


def test_collect_rejects_escape_and_symlink(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    good = root / "a.png"
    good.write_bytes(b"png")
    evil = tmp_path / "evil.png"
    evil.write_bytes(b"x")
    symlink = root / "link.png"
    try:
        symlink.symlink_to(evil)
    except OSError:
        symlink = None
    files = collect_evidence_files([
        str(good), str(evil), "../evil.png", str(symlink) if symlink else "",
    ], root=root)
    assert files == [good]


def test_build_zip_arcname_no_abs(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    f = root / "a.png"
    f.write_bytes(b"png")
    dest = tmp_path / "out.zip"
    path, names = build_evidence_zip([str(f)], dest=dest, root=root)
    assert path.exists()
    with zipfile.ZipFile(path) as zf:
        assert zf.namelist() == ["a.png"]


def test_build_zip_empty_raises(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    with pytest.raises(ValueError):
        build_evidence_zip(["missing.png"], dest=tmp_path / "x.zip", root=root)
