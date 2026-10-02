"""Where secrets go away from Windows: the desktop keyring, or nowhere."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from openachievements import credentials


class FakeSecretTool:
    def __init__(self):
        self.store, self.argvs = {}, []

    def __call__(self, argv, input=None, **kw):
        self.argvs.append(argv)
        verb, key = argv[1], argv[-1]
        if verb == "store":
            self.store[key] = input
            return SimpleNamespace(returncode=0, stdout="")
        if verb == "lookup":
            found = self.store.get(key)
            return SimpleNamespace(returncode=0 if found else 1, stdout=(found + "\n") if found else "")
        self.store.pop(key, None)
        return SimpleNamespace(returncode=0, stdout="")


@pytest.fixture
def posix(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENACHIEVEMENTS_CONFIG", str(tmp_path / "cfg"))
    monkeypatch.delenv(credentials.FILE_OPT_IN, raising=False)
    tool = FakeSecretTool()
    monkeypatch.setattr(credentials.subprocess, "run", tool)
    return tool


def test_the_keyring_gets_the_secret_on_stdin_never_on_the_command_line(posix, monkeypatch):
    monkeypatch.setattr(credentials.shutil, "which", lambda name: "/usr/bin/secret-tool")
    credentials._posix_set("OpenAchievements/psn/p1", "SECRET-VALUE")
    assert credentials._posix_get("OpenAchievements/psn/p1") == "SECRET-VALUE"
    assert not any("SECRET-VALUE" in " ".join(a) for a in posix.argvs)
    assert not credentials._posix_file().exists()
    credentials._posix_delete("OpenAchievements/psn/p1")
    assert credentials._posix_get("OpenAchievements/psn/p1") is None


def test_without_a_keyring_a_secret_is_refused_not_written(posix, monkeypatch):
    monkeypatch.setattr(credentials.shutil, "which", lambda name: None)
    with pytest.raises(credentials.CredentialStoreUnavailable, match="secret-tool"):
        credentials._posix_set("OpenAchievements/psn/p1", "SECRET-VALUE")
    assert not credentials._posix_file().exists()


def test_the_file_is_only_an_explicit_choice(posix, monkeypatch):
    monkeypatch.setattr(credentials.shutil, "which", lambda name: None)
    monkeypatch.setenv(credentials.FILE_OPT_IN, "1")
    credentials._posix_set("OpenAchievements/psn/p1", "SECRET-VALUE")
    assert credentials._posix_get("OpenAchievements/psn/p1") == "SECRET-VALUE"


def test_an_old_file_secret_moves_into_the_keyring(posix, monkeypatch):
    path = credentials._posix_file()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"OpenAchievements/psn/p1": "OLD"}), encoding="utf-8")
    monkeypatch.setattr(credentials.shutil, "which", lambda name: "/usr/bin/secret-tool")
    assert credentials._posix_get("OpenAchievements/psn/p1") == "OLD"
    assert posix.store["OpenAchievements/psn/p1"] == "OLD"
    assert "OLD" not in path.read_text(encoding="utf-8")


def test_the_refusal_reaches_the_page_as_a_normal_error():
    from openachievements.profile import ProfileError
    assert issubclass(credentials.CredentialStoreUnavailable, ProfileError)
