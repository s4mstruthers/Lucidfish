"""The release pipeline's helpers: release notes from the changelog, installer naming, the updater manifest."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "desktop" / "scripts"))

import collect_bundles  # noqa: E402
import make_latest_json  # noqa: E402
import release_notes  # noqa: E402

from lucidfish import __version__  # noqa: E402

CHANGELOG = """# What's new

## 0.5.0

- Later.

## 0.4.0 — the first app

- **Desktop app.** One installer.
- More.

## 0.3.0

Old.
"""


def test_release_notes_are_the_versions_section():
    assert release_notes.section("v0.4.0", CHANGELOG) == "- **Desktop app.** One installer.\n- More.\n"
    assert release_notes.section("0.3.0", CHANGELOG) == "Old.\n"
    assert release_notes.section("0.2.0", CHANGELOG) is None
    assert release_notes.section(__version__) is not None            # the real changelog covers this version


def test_installers_are_collected_with_unambiguous_names(tmp_path, monkeypatch):
    monkeypatch.setattr(collect_bundles, "TAURI", tmp_path / "src-tauri")
    monkeypatch.setattr(collect_bundles, "version", lambda: "0.4.0")
    bundle = tmp_path / "src-tauri" / "target" / "aarch64-apple-darwin" / "release" / "bundle"
    (bundle / "macos").mkdir(parents=True)
    (bundle / "dmg").mkdir()
    (bundle / "macos" / "Lucidfish.app.tar.gz").write_bytes(b"app")
    (bundle / "macos" / "Lucidfish.app.tar.gz.sig").write_text("SIG")
    (bundle / "dmg" / "Lucidfish_0.4.0_aarch64.dmg").write_bytes(b"dmg")
    out = tmp_path / "dist"
    names = [p.name for p in collect_bundles.collect("aarch64-apple-darwin", out)]
    assert names == ["Lucidfish-0.4.0-macOS-arm64.app.tar.gz", "Lucidfish-0.4.0-macOS-arm64.app.tar.gz.sig",
                     "Lucidfish-0.4.0-macOS-arm64.dmg"]
    assert collect_bundles.main(["--target", "x86_64-unknown-linux-gnu", "--out", str(out)]) == 1   # nothing built

    manifest = make_latest_json.manifest("v0.4.0", out, "Notes.")
    assert list(manifest["platforms"]) == ["darwin-aarch64"]
    entry = manifest["platforms"]["darwin-aarch64"]
    assert entry["signature"] == "SIG"
    assert entry["url"].endswith("/releases/download/v0.4.0/Lucidfish-0.4.0-macOS-arm64.app.tar.gz")
    assert manifest["version"] == "0.4.0" and manifest["notes"] == "Notes."
    # Windows: the NSIS installer is what the updater downloads; the MSI is for people.
    (out / "Lucidfish-0.4.0-Windows-x64-setup.exe").write_bytes(b"exe")
    (out / "Lucidfish-0.4.0-Windows-x64-setup.exe.sig").write_text("WSIG")
    (out / "Lucidfish-0.4.0-Windows-x64.msi").write_bytes(b"msi")
    assert set(make_latest_json.manifest("v0.4.0", out)["platforms"]) == {"darwin-aarch64", "windows-x86_64"}
    # Unsigned builds give no manifest at all.
    for sig in out.glob("*.sig"):
        sig.unlink()
    assert make_latest_json.manifest("v0.4.0", out) is None
    assert make_latest_json.main(["v0.4.0", "--dist", str(out)]) == 0 and not (out / "latest.json").exists()


def test_manifest_written_to_disk_is_what_the_updater_reads(tmp_path):
    (tmp_path / "Lucidfish-0.4.0-Linux-x64.AppImage").write_bytes(b"app")
    (tmp_path / "Lucidfish-0.4.0-Linux-x64.AppImage.sig").write_text("LSIG\n")
    notes = tmp_path / "notes.md"
    notes.write_text("- New.\n")
    assert make_latest_json.main(["v0.4.0", "--dist", str(tmp_path), "--notes", str(notes)]) == 0
    data = json.loads((tmp_path / "latest.json").read_text())
    assert data["platforms"] == {"linux-x86_64": {
        "signature": "LSIG",
        "url": "https://github.com/s4mstruthers/Lucidfish/releases/download/v0.4.0/Lucidfish-0.4.0-Linux-x64.AppImage"}}
    assert data["notes"] == "- New." and data["pub_date"].endswith("Z")
