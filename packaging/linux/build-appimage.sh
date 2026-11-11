#!/bin/sh
# Wrap the PyInstaller one-folder build (dist/dbnero) into an AppImage.
set -eu
OUT="${1:-dbnero-linux-x86_64.AppImage}"
APPDIR="$(mktemp -d)/dbnero.AppDir"
mkdir -p "$APPDIR/usr/lib"
cp -r dist/dbnero "$APPDIR/usr/lib/dbnero"
cp packaging/linux/AppRun "$APPDIR/AppRun"
cp packaging/linux/dbnero.desktop "$APPDIR/dbnero.desktop"
cp packaging/icons/dbnero.png "$APPDIR/dbnero.png"
TOOL="$(mktemp -d)/appimagetool"
curl -fsSL -o "$TOOL" \
  https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x "$TOOL"
ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUT"
