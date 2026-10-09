"""The Debian package: recognised by the marker it leaves beside the program,
updated through the system's installer, never writing its own menu entry."""
import hashlib
import io
import sys

import pytest

from openachievements import linux_desktop, self_update, update

DEB = b"!<arch>\ndebian-binary fake package " * 500


@pytest.fixture
def installed(tmp_path, monkeypatch):
    program = tmp_path / "opt" / "greycell-achievements" / "greycell-achievements"
    program.parent.mkdir(parents=True)
    program.write_bytes(b"\x7fELF")
    (program.parent / ".deb-install").write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(program))
    monkeypatch.delenv("APPIMAGE", raising=False)
    self_update.STATE.update(phase=None, error=None, version=None)
    return program


def test_the_package_reads_its_own_entry_in_the_manifest(installed):
    assert update.deb_install()
    assert update.platform_key("linux", "x86_64") == "linux-x86_64-deb"
    assert update.platform_key("linux", "aarch64", deb=False) == "linux-arm64"
    assert update.platform_key("win32", "AMD64") == "windows-x86_64"          # only Linux has packages
    manifest = {"version": "9.1.0", "setup": "https://greycell.app/downloads/greycell-achievements/S.exe",
                "sha256": "a" * 64, "size": 10, "platforms": {
                    "linux-x86_64": {"version": "9.1.0", "url": "https://greycell.app/downloads/greycell-achievements/G.AppImage",
                                     "sha256": "b" * 64, "size": 20},
                    "linux-x86_64-deb": {"version": "9.1.0",
                                         "url": "https://greycell.app/downloads/greycell-achievements/g_9.1.0_amd64.deb",
                                         "sha256": "c" * 64, "size": 30}}}
    found = update.latest_setup(update.MANIFEST, lambda url: manifest, key="linux-x86_64-deb")
    assert found["setup"].endswith("_amd64.deb") and found["size"] == 30


def test_a_checked_package_is_handed_to_the_installer_and_the_app_keeps_running(installed, tmp_path, monkeypatch):
    release = {"version": "9.1.0", "setup": "https://greycell.app/downloads/greycell-achievements/g_9.1.0_amd64.deb",
               "sha256": hashlib.sha256(DEB).hexdigest(), "size": len(DEB)}
    opened, quit = [], []
    monkeypatch.setattr(self_update, "_hand_to_installer", lambda path: opened.append(path))
    assert self_update.install(release, lambda: quit.append(1), lambda url: io.BytesIO(DEB), folder=tmp_path)
    assert opened[0].name == "GreycellAchievements-9.1.0.deb" and opened[0].read_bytes() == DEB
    assert quit == [] and self_update.STATE["phase"] == "handed-over"


def test_without_a_software_installer_it_says_where_the_package_is(tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(self_update.UpdateError, match="sudo apt install"):
        self_update._hand_to_installer(tmp_path / "GreycellAchievements-9.1.0.deb")


def test_the_package_brings_its_own_menu_entry(installed):
    assert linux_desktop.install_menu_entry() is False
