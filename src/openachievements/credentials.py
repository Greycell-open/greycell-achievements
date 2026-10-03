"""Secrets that belong to this computer's user, never to the profile.

Platform sessions and keys (PlayStation, Xbox, RetroAchievements, a sync
server's token) are kept in the operating system's credential store: Windows
Credential Manager through its own API, or on Linux the desktop keyring (Secret
Service) through `secret-tool`, which takes the secret on standard input. They
are never written to the profile folder, never put in an event, never synced,
and never printed.

With no credential store (macOS from source, a headless server), storing a
secret is refused with the reason, unless the person opts in with
OPENACHIEVEMENTS_FILE_SECRETS=1 to a file in the machine config folder that
only they can read. Secrets an older version wrote to that file move into the
keyring the first time they are read.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PREFIX = "OpenAchievements"


from .profile import ProfileError  # noqa: E402 - profile only imports this module inside functions


class CredentialStoreUnavailable(ProfileError):
    """Raised instead of writing a secret somewhere unsafe."""


def _target(name: str, scope: str) -> str:
    return f"{PREFIX}/{name}/{scope}"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _CRED_TYPE_GENERIC, _CRED_PERSIST_LOCAL_MACHINE = 1, 2

    def _set(target: str, secret: str) -> None:
        blob = secret.encode("utf-16-le")
        cred = _CREDENTIAL(Type=_CRED_TYPE_GENERIC, TargetName=target, CredentialBlobSize=len(blob),
                           CredentialBlob=ctypes.cast(ctypes.create_string_buffer(blob, len(blob)),
                                                      ctypes.POINTER(ctypes.c_char)),
                           Persist=_CRED_PERSIST_LOCAL_MACHINE, UserName=PREFIX)
        if not _advapi.CredWriteW(ctypes.byref(cred), 0):
            raise OSError(ctypes.get_last_error(), "could not save to Windows Credential Manager")

    def _get(target: str) -> str | None:
        ptr = ctypes.POINTER(_CREDENTIAL)()
        if not _advapi.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
            return None
        try:
            c = ptr.contents
            return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
        finally:
            _advapi.CredFree(ptr)

    def _delete(target: str) -> None:
        _advapi.CredDeleteW(target, _CRED_TYPE_GENERIC, 0)

    def where() -> str:
        return "Windows Credential Manager"


# ---- elsewhere: the desktop keyring, or nothing ---------------------------------

FILE_OPT_IN = "OPENACHIEVEMENTS_FILE_SECRETS"


def _posix_file() -> Path:
    from .profile import default_config_dir
    return Path(default_config_dir()) / "credentials.json"


def _posix_load() -> dict:
    try:
        return json.loads(_posix_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _posix_save(data: dict) -> None:
    path = _posix_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".credentials.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _posix_keyring(args: list[str], secret: str | None = None):
    """Secret Service through secret-tool, or None when there is none."""
    run = subprocess.run
    tool = shutil.which("secret-tool")
    if not tool:
        return None
    from .linux_desktop import system_env               # the system's libraries, not the AppImage's
    try:
        return run([tool, *args], input=secret, capture_output=True, text=True, timeout=15, env=system_env())
    except (OSError, subprocess.SubprocessError):
        return None


def _posix_attrs(target: str) -> list[str]:
    return ["service", PREFIX, "target", target]


def _posix_no_store() -> "CredentialStoreUnavailable":
    return CredentialStoreUnavailable(
        "No credential store on this computer to keep the sign-in safe. Install your desktop's "
        f"keyring (libsecret, the secret-tool command), or set {FILE_OPT_IN}=1 to accept a file only "
        "you can read in the settings folder.")


def _posix_set(target: str, secret: str) -> None:
    done = _posix_keyring(["store", f"--label={target}", *_posix_attrs(target)], secret)
    if done is not None and done.returncode == 0:
        _posix_file_drop(target)
        return
    if os.environ.get(FILE_OPT_IN) == "1":
        data = _posix_load()
        data[target] = secret
        _posix_save(data)
        return
    raise _posix_no_store()


def _posix_get(target: str) -> str | None:
    found = _posix_keyring(["lookup", *_posix_attrs(target)])
    if found is not None and found.returncode == 0 and found.stdout:
        return found.stdout.rstrip("\n")
    legacy = _posix_load().get(target)
    if legacy and found is not None:               # a keyring now exists: move the old file entry into it
        try:
            _posix_set(target, legacy)
        except CredentialStoreUnavailable:
            pass
    return legacy


def _posix_file_drop(target: str) -> None:
    data = _posix_load()
    if data.pop(target, None) is not None:
        _posix_save(data)


def _posix_delete(target: str) -> None:
    _posix_keyring(["clear", *_posix_attrs(target)])
    _posix_file_drop(target)


def _posix_where() -> str:
    if shutil.which("secret-tool"):
        return "the desktop keyring (Secret Service)"
    return str(_posix_file()) if os.environ.get(FILE_OPT_IN) == "1" else "nowhere: no credential store"


if sys.platform != "win32":
    _set, _get, _delete, where = _posix_set, _posix_get, _posix_delete, _posix_where


def set_secret(name: str, scope: str, secret: str) -> None:
    _set(_target(name, scope), secret)


def get_secret(name: str, scope: str) -> str | None:
    return _get(_target(name, scope))


def delete_secret(name: str, scope: str) -> None:
    _delete(_target(name, scope))
