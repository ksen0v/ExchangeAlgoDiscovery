"""The same dark / light surfaces for native windows and the dashboard."""
from PySide6.QtGui import QColor, QPalette

THEMES = {
    "dark": {"bg":"#080808", "panel":"#101010", "panel2":"#191919", "line":"#292929",
             "text":"#eeeeee", "text2":"#b7b7b7", "muted":"#909090", "accent":"#99b2ff",
             "ink":"#080808", "buy":"#48be92", "sell":"#ee7180", "spot":"#65b8df", "perp":"#ba99ea"},
    "light": {"bg":"#f2f2f2", "panel":"#ffffff", "panel2":"#f4f4f4", "line":"#dddddd",
              "text":"#191919", "text2":"#4c4c4c", "muted":"#686868", "accent":"#3158bd",
              "ink":"#ffffff", "buy":"#13764f", "sell":"#b32d45", "spot":"#14739c", "perp":"#8345b0"},
}


def palette(theme: str) -> QPalette:
    colors = THEMES.get(theme, THEMES["dark"])
    p = QPalette()
    for role, name in {"Window":"panel", "WindowText":"text", "Base":"panel2",
                       "AlternateBase":"panel", "Text":"text", "Button":"panel2", "ButtonText":"text",
                       "ToolTipBase":"panel2", "ToolTipText":"text", "Highlight":"accent",
                       "HighlightedText":"ink", "PlaceholderText":"muted", "Link":"accent"}.items():
        p.setColor(getattr(QPalette.ColorRole, role), QColor(colors[name]))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors["muted"]))
    return p
