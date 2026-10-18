"""Read Omarchy's palette without changing the shell or executing theme code."""

from pathlib import Path
import re
import subprocess
import tomllib

from gi.repository import Gdk, Gtk


DEFAULTS = {
    "background": "#1a1b26", "lighter_background": "#24283b",
    "dark_background": "#13141c", "foreground": "#a9b1d6",
    "bright_foreground": "#c0caf5", "accent": "#7aa2f7",
    "muted": "#414868", "green": "#9ece6a", "red": "#f7768e",
}


class Theme:
    def __init__(self):
        self.provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.signature = None
        self.colors = dict(DEFAULTS)
        self.font = "monospace"
        self.refresh()

    def refresh(self):
        home = Path.home()
        current = home / ".local/state/omarchy/current/theme"
        paths = [current / "colors.toml", current / "shell.toml",
                 home / ".config/omarchy/shell.toml", home / ".config/fontconfig/fonts.conf"]
        signature = []
        for path in paths:
            try:
                info = path.stat()
                signature.append((info.st_ino, info.st_mtime_ns))
            except OSError:
                signature.append(None)
        if signature == self.signature:
            return True
        self.signature = signature
        size = 12
        for path in paths[:3]:
            try:
                values = tomllib.loads(path.read_text())
                if path.name == "colors.toml":
                    for key in self.colors:
                        color = values.get(key)
                        if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                            self.colors[key] = color
                else:
                    value = values.get("font", {}).get("base-size", size)
                    if isinstance(value, (int, float)) and 8 <= value <= 24:
                        size = value
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        try:
            font = subprocess.check_output(
                ["fc-match", "-f", "%{family[0]}", "monospace"], text=True, timeout=2).strip()
            if font and re.fullmatch(r"[\w .+-]+", font):
                self.font = font
        except (OSError, subprocess.SubprocessError):
            pass
        c = self.colors
        self.provider.load_from_string(f"""
            .lookout {{ background: {c['background']}; color: {c['foreground']};
                        font-family: '{self.font}'; font-size: {size}px; }}
            .lookout.panel {{ border: 1px solid {c['accent']}; border-radius: 5px; }}
            .lookout .brand {{ color: {c['bright_foreground']}; font-weight: 700; font-size: 15px; }}
            .lookout .eyebrow {{ color: {c['accent']}; font-size: 10px; letter-spacing: 1px; }}
            .lookout .muted {{ opacity: 0.68; }}
            .lookout .heading {{ color: {c['bright_foreground']}; font-size: 19px; font-weight: 700; }}
            .lookout .error {{ color: {c['red']}; }}
            .lookout .success {{ color: {c['green']}; }}
            .lookout .camera {{ background: {c['dark_background']}; border: 1px solid {c['muted']};
                               border-radius: 4px; }}
            .lookout .camera picture {{ background: #08090d; }}
            .lookout .camera-name {{ color: {c['bright_foreground']}; font-weight: 600; font-size: 11px; }}
            .lookout .camera-status, .lookout .snapshot-status {{ font-size: 9px; padding: 2px 4px;
                background: alpha({c['dark_background']}, 0.88); border-radius: 2px; }}
            .lookout .snapshot-status {{ color: {c['foreground']}; }}
            .lookout picture.stale {{ opacity: 0.65; }}
            .lookout.panel button {{ min-height: 20px; padding: 2px 8px; }}
            .lookout .device {{ background: {c['lighter_background']}; border-radius: 4px; padding: 12px; }}
            .lookout button {{ background: {c['lighter_background']}; color: {c['foreground']};
                               border: 1px solid {c['muted']}; border-radius: 4px;
                               box-shadow: none; min-height: 24px; padding: 4px 10px; }}
            .lookout button:hover {{ border-color: {c['accent']}; color: {c['bright_foreground']}; }}
            .lookout button.primary {{ background: {c['accent']}; color: {c['background']};
                                       border-color: {c['accent']}; font-weight: 600; }}
            .lookout button.flat {{ background: transparent; border-color: transparent; }}
            .lookout button.danger {{ color: {c['red']}; }}
            .lookout entry, .lookout passwordentry {{ background: {c['dark_background']};
                color: {c['foreground']}; border: 1px solid {c['muted']}; border-radius: 4px;
                min-height: 32px; box-shadow: none; }}
            .lookout separator {{ background: {c['muted']}; min-height: 1px; }}
            .lookout scrollbar {{ background: transparent; }}
            .lookout scrollbar slider {{ background: {c['muted']}; min-height: 5px; border-radius: 3px; }}
        """)
        return True
