"""Gera installer/icon.ico a partir do mesmo desenho usado na bandeja."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from screencam.tray import ICON_ACTIVE, _icon  # noqa: E402

out = os.path.join(os.path.dirname(__file__), "..", "installer", "icon.ico")
_icon(ICON_ACTIVE).resize((256, 256)).save(out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
print("icone gerado:", os.path.abspath(out))
