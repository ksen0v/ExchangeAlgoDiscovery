# PyInstaller spec for the desktop app. From the repository root:
#     pyinstaller --noconfirm desktop/radar.spec
# Result: dist/ManipulationRadar/ManipulationRadar.exe (a folder build: starts fast, no unpacking).
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - defined by PyInstaller
ICON = os.path.join(ROOT, "desktop", "assets", "radar.ico")

# ccxt and uvicorn pick modules at runtime (exchange classes, protocol "auto" choices).
hidden = collect_submodules("ccxt") + collect_submodules("uvicorn") + collect_submodules("app")

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "desktop", "__main__.py")],
    pathex=[ROOT],
    hiddenimports=hidden,
    datas=[(os.path.join(ROOT, "static"), "static")] + collect_data_files("ccxt"),
    excludes=["tkinter", "pytest", "PySide6.Qt3DCore", "PySide6.QtQuick3D", "PySide6.QtMultimedia", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtBluetooth", "PySide6.QtSerialPort"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ManipulationRadar",
    console=False,
    icon=ICON,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ManipulationRadar")  # noqa: F821
