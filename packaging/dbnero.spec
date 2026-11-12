# PyInstaller spec for dbnero.  Build with:  pyinstaller packaging/dbnero.spec
# Produces dist/dbnero/ (one-folder build) and, on macOS, dist/dbnero.app.
# External database tools (pg_dump, mysqldump, ODBC driver) are NOT bundled;
# the first-run check tells users how to install them.
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
ICONS = ROOT / "packaging" / "icons"
VERSION = re.search(
    r'__version__ = "([^"]+)"', (ROOT / "src" / "dbnero" / "__init__.py").read_text()
).group(1)

hidden = (
    collect_submodules("dbnero")
    + collect_submodules("keyring.backends")
    + ["psycopg", "psycopg_binary", "mysql.connector", "pyodbc"]
)

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    hiddenimports=hidden,
    datas=[
        (str(ROOT / "src" / "dbnero" / "ui" / "assets"), "dbnero/ui/assets"),
        (str(ROOT / "src" / "dbnero" / "legal" / "TERMS.txt"), "dbnero/legal"),
        (str(ROOT / "src" / "dbnero" / "legal" / "PRIVACY.txt"), "dbnero/legal"),
    ],
    excludes=[
        "tkinter",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore",
        "PySide6.QtQuick",
        "PySide6.QtQml",
    ],
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
    icon=str(ICONS / ("dbnero.ico" if sys.platform == "win32" else "dbnero.icns")),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="dbnero")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="dbnero.app",
        icon=str(ICONS / "dbnero.icns"),
        bundle_identifier="io.github.oguzcanozdemir.dbnero",
        info_plist={
            "CFBundleName": "dbnero",
            "CFBundleDisplayName": "dbnero",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
