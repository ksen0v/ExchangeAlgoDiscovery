"""Self-contained updater/launcher, built only on GitHub. No user Python required."""
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
a = Analysis([os.path.join(ROOT, "desktop", "launcher.py")], pathex=[ROOT],
             binaries=[], datas=[], hiddenimports=[],
             excludes=["PySide6", "ccxt", "app", "aiohttp", "pytest", "tkinter"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="ManipulationRadar", console=False,
          icon=os.path.join(ROOT, "desktop", "assets", "radar.ico"))
