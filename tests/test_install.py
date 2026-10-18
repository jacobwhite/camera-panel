import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch
import zipfile

import install as installer


ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="camera-panel-install-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.source = self.base / "checkout"
        self.source.mkdir()
        shutil.copytree(ROOT / "lookout", self.source / "lookout",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "packaging", self.source / "packaging")
        environment = patch.dict(os.environ, {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        # Replace only the installer's subprocess binding, never the test runner's.
        self.process = Mock()
        self.process.CalledProcessError = subprocess.CalledProcessError
        self.process.run.return_value = subprocess.CompletedProcess([], 0)
        process = patch.object(installer, "subprocess", self.process)
        process.start()
        self.addCleanup(process.stop)
        which = patch.object(installer.shutil, "which", return_value=None)
        self.which = which.start()
        self.addCleanup(which.stop)
        self.prefix = self.home / ".local"
        self.app = self.prefix / "share/camera-panel"
        self.launcher = self.prefix / "bin/camera-panel"
        self.desktop = self.prefix / "share/applications/camera-panel.desktop"
        self.icon = self.prefix / "share/icons/hicolor/scalable/apps/camera-panel.svg"
        self.service = self.home / ".config/systemd/user/camera-panel.service"

    def install(self, **options):
        return installer.install(**{"source": self.source, "no_start": True,
                                    "check_dependencies": False, **options})

    def snapshot(self):
        return {str(path.relative_to(self.home)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in self.home.rglob("*") if path.is_file()}

    def test_installed_copy_and_assets_have_no_checkout_references(self):
        paths = self.install()
        for source in (self.source / "lookout").glob("*.py"):
            self.assertEqual((self.app / "lookout" / source.name).read_bytes(), source.read_bytes())
        svg = (self.source / "lookout/lookout.svg").read_bytes()
        self.assertEqual((self.app / "lookout/lookout.svg").read_bytes(), svg)
        self.assertEqual(self.icon.read_bytes(), svg)
        for name in ("launcher", "desktop", "service", "manifest"):
            self.assertNotIn(str(self.source), paths[name].read_text())
            self.assertNotIn(str(ROOT), paths[name].read_text())
        self.assertIn(f"sys.path.insert(0, {str(self.app)!r})", self.launcher.read_text())
        self.assertIn(f'Exec="{self.launcher}" --settings', self.desktop.read_text())
        self.assertIn(f"Icon={self.icon}", self.desktop.read_text())
        self.assertIn(f'ExecStart={installer.SYSTEM_PYTHON} "{self.launcher}" --background', self.service.read_text())
        self.assertEqual(stat.S_IMODE(self.launcher.stat().st_mode), 0o755)
        for path in (self.desktop, self.service, self.icon, self.app / "lookout/__main__.py"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.process.run.assert_not_called()

    def test_launcher_runs_after_checkout_is_removed(self):
        self.install()
        shutil.rmtree(self.source)
        result = subprocess.run([str(self.launcher), "--help"], cwd=self.base,
                                capture_output=True, text=True, check=True)
        self.assertIn("--background", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(list(self.app.rglob("__pycache__")), [])

    def test_idempotent_install_after_running_launcher(self):
        self.install()
        before = self.snapshot()
        subprocess.run([str(self.launcher), "--help"], cwd=self.base, capture_output=True, check=True)
        self.install()
        self.assertEqual(self.snapshot(), before)

    def test_updates_and_removes_only_unchanged_owned_files(self):
        retired = self.source / "lookout/retired"
        retired.mkdir()
        (retired / "__init__.py").write_text("# retired\n")
        self.install()
        shutil.rmtree(retired)
        updated = self.source / "lookout/__init__.py"
        updated.write_text("# updated application\n")
        (self.source / "lookout/added.py").write_text("# new module\n")
        self.install()
        self.assertFalse((self.app / "lookout/retired").exists())
        self.assertEqual((self.app / "lookout/__init__.py").read_bytes(), updated.read_bytes())
        self.assertTrue((self.app / "lookout/added.py").is_file())
        self.install()

    def test_missing_owned_files_are_restored(self):
        self.install()
        self.icon.unlink()
        (self.app / "lookout/__main__.py").unlink()
        self.install()
        self.assertTrue(self.icon.is_file())
        self.assertTrue((self.app / "lookout/__main__.py").is_file())

    def test_unrelated_destinations_refused_before_writing(self):
        for path in (self.launcher, self.desktop, self.icon, self.service):
            with self.subTest(path=path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(installer.MARKER + "\n# not owned without a manifest\n")
                before = self.snapshot()
                with self.assertRaisesRegex(installer.InstallError, "unrelated"):
                    self.install()
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.app.exists())
                path.unlink()

    def test_unowned_application_directory_is_refused(self):
        self.app.mkdir(parents=True)
        with self.assertRaisesRegex(installer.InstallError, "unowned"):
            self.install()
        self.assertEqual(list(self.app.iterdir()), [])
        self.assertFalse(self.launcher.exists())

    def test_modified_installed_files_are_preserved(self):
        self.install()
        for path in (self.launcher, self.desktop, self.service, self.icon, self.app / "lookout/__main__.py"):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b"\n# local changes\n")
                before = self.snapshot()
                with self.assertRaisesRegex(installer.InstallError, "modified"):
                    self.install()
                self.assertEqual(self.snapshot(), before)
                path.write_bytes(original)

    def test_modified_permissions_are_preserved(self):
        self.install()
        self.launcher.chmod(0o700)
        before = self.snapshot()
        with self.assertRaisesRegex(installer.InstallError, "modified"):
            self.install()
        self.assertEqual(self.snapshot(), before)

    def test_untracked_application_files_and_directories_are_preserved(self):
        self.install()
        for name, directory in (("notes.txt", False), ("personal", True)):
            with self.subTest(name=name):
                path = self.app / name
                if directory:
                    path.mkdir()
                else:
                    path.write_text("unrelated user content")
                before = self.snapshot()
                with self.assertRaisesRegex(installer.InstallError, "unrelated application content"):
                    self.install()
                self.assertEqual(self.snapshot(), before)
                self.assertTrue(path.exists())
                path.rmdir() if directory else path.unlink()

    def test_symlink_destinations_and_parent_directories_are_refused(self):
        target = self.base / "unrelated"
        target.mkdir()
        for path in (self.launcher, self.service, self.app, self.prefix / "bin"):
            with self.subTest(path=path):
                if path.is_dir():
                    path.rmdir()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(target, target_is_directory=True)
                with self.assertRaisesRegex(installer.InstallError, "symlink"):
                    self.install()
                self.assertEqual(list(target.iterdir()), [])
                self.assertTrue(path.is_symlink())
                path.unlink()

    def test_symlinks_inside_owned_application_are_refused(self):
        self.install()
        link = self.app / "personal"
        target = self.base / "unrelated"
        target.mkdir()
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, "unrelated application content"):
            self.install()
        self.assertTrue(link.is_symlink())
        self.assertEqual(list(target.iterdir()), [])

    def test_invalid_manifest_cannot_claim_unrelated_paths(self):
        paths = self.install()
        manifest = paths["manifest"]
        original = json.loads(manifest.read_text())
        sentinel = self.home / "personal.txt"
        sentinel.write_text("keep me\n")
        hostile = {**original, "files": {**original["files"], str(sentinel): installer._record(sentinel.read_bytes(), 0o644)}}
        for content in ("not JSON", "[]", "{}", json.dumps(hostile)):
            with self.subTest(content=content[:40]):
                manifest.write_text(content)
                before = self.snapshot()
                with self.assertRaises(installer.InstallError):
                    self.install()
                self.assertEqual(self.snapshot(), before)

    def test_legacy_lookout_and_user_data_are_never_modified(self):
        legacy = [self.prefix / "bin/lookout", self.prefix / "share/applications/lookout.desktop",
                  self.home / ".config/systemd/user/lookout.service",
                  self.home / ".config/lookout/config.json", self.home / ".cache/lookout/preview.png"]
        for path in legacy:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"# Installed by Lookout\nold contents\n")
        before = {path: path.read_bytes() for path in legacy}
        self.install()
        self.install()
        self.assertEqual({path: path.read_bytes() for path in legacy}, before)
        self.process.run.assert_not_called()

    def test_service_start_uses_only_camera_panel(self):
        self.install(no_start=False)
        self.assertEqual([call.args[0] for call in self.process.run.call_args_list], [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "camera-panel.service"],
            ["systemctl", "--user", "restart", "camera-panel.service"],
        ])

    def test_dependency_check_is_explicit_and_does_not_import_application(self):
        self.which.side_effect = lambda command: "/usr/bin/ffmpeg" if command == "ffmpeg" else None
        self.install(check_dependencies=True)
        self.process.run.assert_called_once()
        command = self.process.run.call_args.args[0]
        self.assertEqual(command[:2], [installer.SYSTEM_PYTHON, "-c"])
        self.assertIn("gi.require_version", command[2])
        self.assertIn("import gi, requests", command[2])

    def test_missing_dependencies_fail_without_installing(self):
        for returncode, ffmpeg in ((1, "/usr/bin/ffmpeg"), (0, None)):
            with self.subTest(returncode=returncode, ffmpeg=ffmpeg):
                self.process.run.return_value = subprocess.CompletedProcess([], returncode)
                self.which.return_value = ffmpeg
                with self.assertRaisesRegex(installer.InstallError, "Missing system dependencies"):
                    self.install(check_dependencies=True)
                self.assertFalse(self.app.exists())
                self.assertFalse(self.launcher.exists())

    def test_prefix_and_xdg_config_home(self):
        prefix = self.home / "custom prefix"
        config = self.home / "custom config"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}):
            paths = self.install(prefix=prefix)
        self.assertEqual(paths["launcher"], prefix / "bin/camera-panel")
        self.assertEqual(paths["service"], config / "systemd/user/camera-panel.service")
        self.assertFalse(self.launcher.exists())
        self.assertFalse(self.service.exists())

    def test_relative_xdg_config_home_uses_home_fallback(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative"}):
            self.install()
        self.assertTrue(self.service.is_file())

    def test_paths_with_spaces_quotes_and_specifiers_are_escaped(self):
        prefix = self.home / 'space " quote \\ slash $dollar %spec `tick'
        paths = self.install(prefix=prefix)
        desktop = paths["desktop"].read_text()
        service = paths["service"].read_text()
        self.assertIn('\\\\"', desktop)
        self.assertIn('\\\\\\\\', desktop)
        self.assertIn('\\\\$dollar %%spec \\\\`tick', desktop)
        self.assertIn('\\"', service)
        self.assertIn('$$dollar %%spec `tick', service)
        subprocess.run([str(paths["launcher"]), "--help"], cwd=self.base, capture_output=True, check=True)
        self.install(prefix=prefix)

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-utils is optional")
    def test_generated_desktop_validates_with_special_path_characters(self):
        paths = self.install(prefix=self.home / 'space " quote \\ slash $dollar %spec `tick')
        result = subprocess.run(["desktop-file-validate", str(paths["desktop"])],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd validation is optional")
    def test_generated_service_validates_with_special_path_characters(self):
        runtime = self.base / "runtime"
        runtime.mkdir(mode=0o700)
        paths = self.install(prefix=self.home / 'space " quote \\ slash $dollar %spec `tick')
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            result = subprocess.run(["systemd-analyze", "--user", "verify", str(paths["service"])],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_control_characters_in_prefix_are_refused(self):
        with self.assertRaisesRegex(installer.InstallError, "Unsupported installation path"):
            self.install(prefix=self.home / "bad\npath")
        self.assertEqual(self.snapshot(), {})

    def test_cli_no_start_with_isolated_home_and_stub_systemctl(self):
        stub_bin = self.base / "stub-bin"
        stub_bin.mkdir()
        stub = stub_bin / "systemctl"
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/systemctl.log"\n')
        stub.chmod(0o755)
        environment = {**os.environ, "PATH": str(stub_bin)}
        command = [sys.executable, str(ROOT / "install.py"), "--skip-dependency-check",
                   "--prefix", str(self.prefix)]
        result = subprocess.run([*command, "--no-start"], cwd=self.base, env=environment,
                                capture_output=True, text=True, check=True)
        self.assertIn("Legacy Lookout", result.stdout)
        self.assertFalse((self.home / "systemctl.log").exists())
        subprocess.run(command, cwd=self.base, env=environment, capture_output=True, check=True)
        self.assertEqual((self.home / "systemctl.log").read_text().splitlines(), [
            "--user daemon-reload", "--user enable camera-panel.service", "--user restart camera-panel.service",
        ])
        self.assertFalse((self.home / ".config/lookout").exists())

    def test_cli_reports_ownership_error(self):
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("unrelated")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as caught:
                installer.main(["--no-start", "--skip-dependency-check"])
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("unrelated", error.getvalue())
        self.assertEqual(self.launcher.read_text(), "unrelated")


class PackagingTests(unittest.TestCase):
    @unittest.skipUnless(all(importlib.util.find_spec(name) for name in ("build", "setuptools", "wheel", "installer")),
                         "optional distribution build tools are not installed")
    def test_wheel_sdist_and_staged_wheel_install(self):
        with tempfile.TemporaryDirectory(prefix="camera-panel-build-test-") as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            for name in ("lookout", "packaging", "tests"):
                shutil.copytree(ROOT / name, source / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for name in ("pyproject.toml", "MANIFEST.in", "README.md", "install.py", "camera-panel", "LICENSE"):
                if (ROOT / name).exists():
                    shutil.copy2(ROOT / name, source / name)
            home = base / "home"
            home.mkdir()
            environment = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                           "PYTHONDONTWRITEBYTECODE": "1", "PATH": os.defpath,
                           "TMPDIR": str(base), "PIP_CONFIG_FILE": os.devnull}
            result = subprocess.run([sys.executable, "-m", "build", "--no-isolation"], cwd=source,
                                    env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            wheel = next((source / "dist").glob("*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                names = archive.namelist()
                self.assertIn("lookout/lookout.svg", names)
                self.assertIn("lookout/__main__.py", names)
                metadata = archive.read(next(name for name in names if name.endswith("/METADATA"))).decode()
                self.assertIn("Name: camera-panel\n", metadata)
                self.assertIn("Version: 0.1.0\n", metadata)
                self.assertIn("License-Expression: Unlicense\n", metadata)
                self.assertIn("Requires-Dist: requests\n", metadata)
                entry_points = archive.read(next(name for name in names if name.endswith("/entry_points.txt"))).decode()
                self.assertIn("camera-panel = lookout.__main__:main", entry_points)
                self.assertFalse(any(name.startswith(("tests/", "packaging/")) for name in names))
                self.assertTrue(any(name.endswith("/licenses/LICENSE") for name in names))
            with tarfile.open(next((source / "dist").glob("*.tar.gz"))) as archive:
                names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
                self.assertTrue({"camera-panel", "install.py", "lookout/lookout.svg", "tests/test_install.py",
                                  "packaging/camera-panel.desktop", "packaging/camera-panel.service"}.issubset(names))
                # The downstream recipe hashes this source archive, so it cannot be inside it.
                self.assertNotIn("packaging/PKGBUILD", names)
            stage = base / "stage"
            result = subprocess.run([sys.executable, "-m", "installer", "--destdir", str(stage), str(wheel)],
                                    cwd=base, env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            library = stage / Path(sysconfig.get_path("purelib")).relative_to("/")
            launcher = stage / Path(sysconfig.get_path("scripts")).relative_to("/") / "camera-panel"
            environment["PYTHONPATH"] = str(library)
            shutil.rmtree(source)
            result = subprocess.run([str(launcher), "--help"], cwd=base, env=environment,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("--background", result.stdout)
            self.assertTrue((library / "lookout/lookout.svg").is_file())

    def test_python_metadata_and_package_data(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        project = metadata["project"]
        self.assertEqual(project["name"], "camera-panel")
        self.assertEqual(project["version"], "0.1.0")
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(project["license"], "Unlicense")
        self.assertEqual(project["scripts"], {"camera-panel": "lookout.__main__:main"})
        self.assertEqual(project["dependencies"], ["requests"])
        self.assertEqual(metadata["tool"]["setuptools"]["package-data"], {"lookout": ["lookout.svg"]})

    def test_arch_assets_and_recipe_do_not_enable_service_or_firewall(self):
        service = (ROOT / "packaging/camera-panel.service").read_text()
        desktop = (ROOT / "packaging/camera-panel.desktop").read_text()
        self.assertIn("ExecStart=/usr/bin/camera-panel --background", service)
        self.assertIn("Name=Camera Panel", desktop)
        self.assertIn("Exec=camera-panel --settings", desktop)
        self.assertIn("Icon=camera-panel", desktop)
        recipe_path = ROOT / "packaging/PKGBUILD"
        if not recipe_path.exists():
            return  # The downstream recipe is deliberately excluded from source distributions.
        recipe = recipe_path.read_text()
        self.assertIn("/usr/lib/systemd/user/camera-panel.service", recipe)
        self.assertIn("/usr/share/icons/hicolor/scalable/apps/camera-panel.svg", recipe)
        self.assertNotIn("systemctl", recipe)
        self.assertNotIn("ufw allow", recipe)
        self.assertNotIn("sha256sums=('SKIP')", recipe)


if __name__ == "__main__":
    unittest.main()
