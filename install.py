#!/usr/bin/env python3
"""Install a standalone user-local copy of Camera Panel, not a checkout link.

The default prefix is ~/.local; --prefix changes bin/ and share/ destinations.
The user unit always goes in $XDG_CONFIG_HOME/systemd/user (default ~/.config).
System Python 3.11+, PyGObject, requests, GTK4, gtk4-layer-shell, libsecret and
FFmpeg must be installed separately. Legacy Lookout files and all user data are
left untouched. Ownership checks refuse local edits rather than overwriting them.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile


MARKER = "# Installed by Camera Panel"
MANIFEST = ".camera-panel-install.json"
SYSTEM_PYTHON = "/usr/bin/python3"


class InstallError(RuntimeError):
    """Installation cannot safely proceed."""


def _check_path(path):
    if ".." in path.parts or any(ord(char) < 32 or ord(char) == 127 for char in str(path)):
        raise InstallError(f"Unsupported installation path: {path}")
    for item in (path, *path.parents):
        if item.is_symlink():
            raise InstallError(f"Refusing a symlink installation path: {item}")
        if item != path and item.exists() and not item.is_dir():
            raise InstallError(f"Not an installation directory: {item}")


def _record(data, mode):
    return {"sha256": hashlib.sha256(data).hexdigest(), "mode": mode}


def _write_file(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install(*, source=None, prefix=None, no_start=False, check_dependencies=True):
    """Install files and optionally enable/restart the user service; return paths.

    source and check_dependencies allow isolated tests without GI or a session bus.
    No systemctl commands are issued with no_start=True.
    """
    source = Path(source) if source is not None else Path(__file__).resolve().parent
    home = Path.home()
    prefix = Path(prefix).expanduser().absolute() if prefix is not None else home / ".local"
    config = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not config.is_absolute():
        config = home / ".config"
    app = prefix / "share/camera-panel"
    manifest = app / MANIFEST
    launcher = prefix / "bin/camera-panel"
    desktop = prefix / "share/applications/camera-panel.desktop"
    icon = prefix / "share/icons/hicolor/scalable/apps/camera-panel.svg"
    service = config / "systemd/user/camera-panel.service"
    external = {launcher, desktop, icon, service}

    if check_dependencies:
        check = subprocess.run([SYSTEM_PYTHON, "-c", """
import sys
assert sys.version_info >= (3, 11)
import gi, requests
for name, version in [('Gtk', '4.0'), ('Gtk4LayerShell', '1.0'), ('Secret', '1')]:
    gi.require_version(name, version)
from gi.repository import Gtk
assert (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 12)
"""], capture_output=True)
        if check.returncode or not shutil.which("ffmpeg"):
            raise InstallError("Missing system dependencies: Python 3.11+, python-gobject, "
                               "python-requests, GTK 4.12+, gtk4-layer-shell, libsecret, ffmpeg")

    package = source / "lookout"
    for required in (package / "__init__.py", package / "__main__.py", package / "lookout.svg"):
        if not required.is_file():
            raise InstallError(f"Missing application source: {required}")
    files = {}
    for path in sorted(package.rglob("*")):
        if path.suffix == ".py" or path == package / "lookout.svg":
            if path.is_symlink() or not path.is_file():
                raise InstallError(f"Not a regular source file: {path}")
            files[app / "lookout" / path.relative_to(package)] = (path.read_bytes(), 0o644)

    wrapper = (f"#!{SYSTEM_PYTHON}\n{MARKER}\nimport os\nimport sys\n"
               "sys.dont_write_bytecode = True\n"
               "os.environ['PYTHONDONTWRITEBYTECODE'] = '1'\n"
               f"sys.path.insert(0, {str(app)!r})\n"
               "from lookout.__main__ import main\nraise SystemExit(main())\n")
    # Desktop Exec values have both key-file escaping and command-line escaping.
    desktop_exec = '"' + ''.join('\\' + char if char in '\\"`$' else char
                                 for char in str(launcher)).replace('%', '%%') + '"'
    desktop_exec = desktop_exec.replace('\\', '\\\\')
    # systemd restricts executable names more than arguments, even when quoted.
    service_exec = (f'{SYSTEM_PYTHON} "' + str(launcher).replace('\\', '\\\\').replace('"', '\\"')
                    .replace('%', '%%').replace('$', '$$') + '"')
    desktop_text = (source / "packaging/camera-panel.desktop").read_text(encoding="utf-8")
    desktop_text = desktop_text.replace("Exec=camera-panel", f"Exec={desktop_exec}")
    desktop_text = desktop_text.replace("Icon=camera-panel", "Icon=" + str(icon).replace('\\', '\\\\'))
    service_text = (source / "packaging/camera-panel.service").read_text(encoding="utf-8")
    service_text = service_text.replace("ExecStart=/usr/bin/camera-panel", f"ExecStart={service_exec}")
    files.update({
        launcher: (wrapper.encode("utf-8"), 0o755),
        desktop: ((MARKER + "\n" + desktop_text).encode("utf-8"), 0o644),
        icon: ((package / "lookout.svg").read_bytes(), 0o644),
        service: ((MARKER + "\n" + service_text).encode("utf-8"), 0o644),
    })

    # Preflight every destination before changing anything. A comment alone does
    # not establish ownership: prior contents and permissions must still match.
    for path in (*files, manifest):
        _check_path(path)
    previous = {}
    if app.exists():
        if not manifest.is_file():
            raise InstallError(f"Refusing an unowned application directory: {app}")
        try:
            saved = json.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as error:
            raise InstallError(f"Invalid ownership manifest: {manifest}") from error
        if (not isinstance(saved, dict) or set(saved) != {"owner", "format", "files"}
                or saved["owner"] != MARKER or saved["format"] != 1
                or not isinstance(saved["files"], dict)):
            raise InstallError(f"Invalid ownership manifest: {manifest}")
        for name, record in saved["files"].items():
            path = Path(name)
            if (not path.is_absolute() or path == app or path == manifest
                    or (path not in external and not path.is_relative_to(app))):
                raise InstallError(f"Invalid owned path in manifest: {name}")
            _check_path(path)
            previous[path] = record
        directories = {parent for path in previous for parent in path.parents
                       if parent.is_relative_to(app)}
        for path in app.rglob("*"):
            if path.is_symlink() or (path != manifest and path not in previous
                                     and not (path.is_dir() and path in directories)):
                raise InstallError(f"Refusing unrelated application content: {path}")

    for path in files.keys() | previous.keys():
        if path.exists():
            if path not in previous or not path.is_file():
                raise InstallError(f"Refusing to replace an unrelated file: {path}")
            if _record(path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) != previous[path]:
                raise InstallError(f"Refusing to replace a modified installed file: {path}")

    records = {str(path): _record(data, mode) for path, (data, mode) in files.items()}
    for path, (data, mode) in files.items():
        if not path.exists() or records[str(path)] != previous.get(path):
            _write_file(path, data, mode)
    stale = previous.keys() - files.keys()
    for path in stale:
        path.unlink(missing_ok=True)
    old_directories = {parent for path in stale for parent in path.parents
                       if parent.is_relative_to(app) and parent != app}
    new_directories = {parent for path in files for parent in path.parents}
    for directory in sorted(old_directories - new_directories, key=lambda path: len(path.parts), reverse=True):
        if directory.exists():
            directory.rmdir()
    data = json.dumps({"owner": MARKER, "format": 1, "files": records}, indent=2, sort_keys=True) + "\n"
    _write_file(manifest, data.encode("utf-8"), 0o644)

    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(desktop.parent)], check=True)
    if not no_start:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "camera-panel.service"], check=True)
        subprocess.run(["systemctl", "--user", "restart", "camera-panel.service"], check=True)
    return {"application": app, "launcher": launcher, "desktop": desktop,
            "icon": icon, "service": service, "manifest": manifest}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-start", action="store_true", help="Do not reload, enable or start the user service")
    parser.add_argument("--prefix", type=Path, help="Install bin/ and share/ here (default: ~/.local); unit stays in XDG_CONFIG_HOME")
    parser.add_argument("--skip-dependency-check", action="store_true", help="Skip system runtime checks, e.g. for an offline staged install")
    args = parser.parse_args(argv)
    try:
        paths = install(prefix=args.prefix, no_start=args.no_start,
                        check_dependencies=not args.skip_dependency_check)
    except (InstallError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Installation failed: {error}\n")
    for kind, path in paths.items():
        print(f"Installed {kind}: {path}")
    print("Legacy Lookout launchers, service and user data were left untouched.")
    if not args.no_start:
        print("Camera Panel is running and enabled for graphical-session login.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
