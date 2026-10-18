import os
from pathlib import Path
import sys


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: camera-panel [--background | --settings | --demo | --version]\n\n"
              "Default: toggle the camera strip. --background: tray only.\n"
              "--settings: manage cameras and NVRs. --demo: isolated local test-pattern preview.")
        return 0
    if "--version" in sys.argv:
        from . import __version__
        print(f"Camera Panel {__version__}")
        return 0
    unknown = set(sys.argv[1:]) - {"--background", "--settings", "--demo"}
    if unknown:
        print("Unknown option. Run camera-panel --help.", file=sys.stderr)
        return 2
    # Layer shell must interpose Wayland symbols before GTK loads libwayland.
    preload = os.environ.get("LD_PRELOAD", "")
    if "libgtk4-layer-shell.so" not in preload:
        os.environ["LD_PRELOAD"] = "libgtk4-layer-shell.so" + (":" + preload if preload else "")
        os.environ["GDK_BACKEND"] = "wayland"
        root = str(Path(__file__).resolve().parent.parent)
        os.environ["PYTHONPATH"] = root + os.pathsep + os.environ.get("PYTHONPATH", "")
        os.execv(sys.executable, [sys.executable, "-m", "lookout", *sys.argv[1:]])
    from .app import Application
    return Application(demo="--demo" in sys.argv).run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
