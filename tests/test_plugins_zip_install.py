"""install_from_zip safety + atomicity."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest


def _make_zip(files: dict[str, bytes | str], symlinks: list[tuple[str, str]] | None = None) -> bytes:
    """Build an in-memory zip. files = {name: content}. symlinks = [(name, target)]."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            zf.writestr(name, data)
        for name, target in symlinks or []:
            info = zipfile.ZipInfo(name)
            # Symlink type bit (POSIX 0o120000) in upper 16 bits of external_attr.
            info.external_attr = (0o120777 & 0xFFFF) << 16
            zf.writestr(info, target)
    return buf.getvalue()


def _good_plugin_json() -> str:
    return json.dumps({
        "schema_version": 1,
        "name": "Demo Plugin",
        "description": "x",
        "version": "1.0.0",
        "category": "utility",
        "runtime": {"mode": "registry", "package": "uvx:demo@1.0.0"},
    })


def test_install_from_zip_happy_path(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    zip_bytes = _make_zip({"plugin.json": _good_plugin_json()})
    final = install_from_zip(zip_bytes, tmp_path)
    assert (final / "plugin.json").exists()
    assert (final / "plugin.json").read_text().strip().startswith("{")


def test_install_from_zip_rejects_path_traversal(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    zip_bytes = _make_zip({
        "../escape.txt": b"x",
        "plugin.json": _good_plugin_json(),
    })
    with pytest.raises(InstallError, match="escape|traversal"):
        install_from_zip(zip_bytes, tmp_path)


def test_install_from_zip_rejects_absolute_path(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    zip_bytes = _make_zip({
        "/etc/passwd": b"x",
        "plugin.json": _good_plugin_json(),
    })
    with pytest.raises(InstallError):
        install_from_zip(zip_bytes, tmp_path)


def test_install_from_zip_rejects_symlink(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    zip_bytes = _make_zip(
        {"plugin.json": _good_plugin_json()},
        symlinks=[("link.txt", "/etc/passwd")],
    )
    with pytest.raises(InstallError, match="symlink"):
        install_from_zip(zip_bytes, tmp_path)


def test_install_from_zip_rejects_oversize_compressed(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    big = b"x" * (51 * 1024 * 1024)  # 51 MB compressed (raw bytes; not a zip)
    with pytest.raises(InstallError, match="too large|size"):
        install_from_zip(big, tmp_path)


def test_install_from_zip_rejects_oversize_uncompressed(tmp_path: Path):
    """A zip-bomb scenario: small compressed, huge declared uncompressed.

    Adaptation note: ``zipfile.ZipFile.writestr`` overwrites a manually-set
    ``file_size`` with the actual data length, so we can't construct the
    bomb via the high-level API. We forge the central-directory uncompressed
    size field directly to simulate a malicious bundle.
    """
    import struct
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.bin", b"x" * 100)
        zf.writestr("plugin.json", _good_plugin_json())
    data = bytearray(buf.getvalue())
    # Locate the CD entry for big.bin (filename appears in the CD header
    # 46 bytes after the signature). Patch the uncompressed-size field at
    # CD offset 24 to declare 201 MB.
    cd_sig = b"PK\x01\x02"
    cd_offset = data.find(cd_sig)
    name_offset = data.find(b"big.bin", cd_offset)
    entry_start = name_offset - 46
    struct.pack_into("<I", data, entry_start + 24, 201 * 1024 * 1024)
    with pytest.raises(InstallError, match="too large|uncompressed"):
        install_from_zip(bytes(data), tmp_path)


def test_install_from_zip_rejects_missing_plugin_json(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    zip_bytes = _make_zip({"README.md": "no plugin.json here"})
    with pytest.raises(InstallError, match="plugin.json"):
        install_from_zip(zip_bytes, tmp_path)


def test_install_from_zip_rejects_invalid_plugin_json(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    zip_bytes = _make_zip({"plugin.json": "{not json"})
    with pytest.raises(InstallError, match="plugin.json|JSON"):
        install_from_zip(zip_bytes, tmp_path)


def test_install_from_zip_atomic_via_staging(tmp_path: Path):
    """If validation passes and rename succeeds, no .installing dir leaks."""
    from glados.plugins.store import install_from_zip
    zip_bytes = _make_zip({"plugin.json": _good_plugin_json()})
    install_from_zip(zip_bytes, tmp_path)
    leftover = list(tmp_path.glob("*.installing"))
    assert leftover == []


def test_install_from_zip_collision_appends_suffix(tmp_path: Path):
    """If demo-plugin/ exists, second install lands at demo-plugin-2/."""
    from glados.plugins.store import install_from_zip
    zip_bytes = _make_zip({"plugin.json": _good_plugin_json()})
    first = install_from_zip(zip_bytes, tmp_path)
    second = install_from_zip(zip_bytes, tmp_path)
    assert first != second
    assert second.name.endswith("-2")
