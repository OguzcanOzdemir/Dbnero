# PyInstaller spec for dbnero.  Build with:  pyinstaller packaging/dbnero.spec
# Produces dist/dbnero/ (one-folder build) and, on macOS, dist/dbnero.app.
# External database tools (pg_dump, mysqldump, ODBC driver) are NOT bundled;
# the first-run check tells users how to install them.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
hidden = (
    collect_submodules("dbnero")
    + collect_submodules("keyring.backends")
    + ["psycopg", "psycopg_binary", "mysql.connector", "pyodbc"]
)

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    hiddenimports=hidden,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="dbnero",
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="dbnero")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="dbnero.app",
        bundle_identifier="io.github.dbnero",
        info_plist={"NSHighResolutionCapable": True, "CFBundleShortVersionString": "0.1.0"},
    )
