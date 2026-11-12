"""Copy database client tools and the libraries they need into a build.

Usage:  python packaging/bundle_tools.py <tools-dir> <engine>=<bin-dir> ...

Example (Linux):
    python packaging/bundle_tools.py dist/dbnero/tools \
        postgresql=/usr/lib/postgresql/18/bin mysql=/usr/bin

Result:  <tools-dir>/<engine>/bin/<tool>  (+ <tools-dir>/<engine>/lib on macOS/Linux)

* Windows: the executable plus every DLL next to it (EDB and MySQL zips ship
  their DLLs in the same folder).
* macOS: libraries are copied and their install names rewritten with
  ``dylibbundler``; everything is ad-hoc signed afterwards by the workflow.
* Linux: non-glibc libraries reported by ``ldd`` are copied and every file
  gets an ``$ORIGIN`` rpath with ``patchelf``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

TOOLS = {
    "postgresql": ["pg_dump", "pg_restore"],
    "mysql": ["mysqldump", "mysql"],
}

# Libraries every Linux desktop has; bundling them would break compatibility.
LINUX_SYSTEM_LIBS = (
    "linux-vdso",
    "ld-linux",
    "libc.so",
    "libm.so",
    "libdl.so",
    "libpthread.so",
    "librt.so",
    "libresolv.so",
    "libgcc_s.so",
    "libstdc++.so",
    "libz.so",
)


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def bundle_windows(src: Path, dest: Path, tools: list[str]) -> None:
    bin_dir = dest / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in tools:
        shutil.copy2(src / f"{tool}.exe", bin_dir / f"{tool}.exe")
    for dll in src.glob("*.dll"):
        shutil.copy2(dll, bin_dir / dll.name)


def bundle_macos(src: Path, dest: Path, tools: list[str]) -> None:
    bin_dir, lib_dir = dest / "bin", dest / "lib"
    bin_dir.mkdir(parents=True, exist_ok=True)
    lib_dir.mkdir(parents=True, exist_ok=True)
    args = ["dylibbundler", "-of", "-b", "-d", str(lib_dir), "-p", "@executable_path/../lib/"]
    for tool in tools:
        target = bin_dir / tool
        shutil.copy2(src / tool, target)
        os.chmod(target, 0o755)
        args += ["-x", str(target)]
    run(*args)


def _linux_deps(binary: Path) -> list[Path]:
    deps = []
    for line in run("ldd", str(binary)).splitlines():
        parts = line.split("=>")
        if len(parts) != 2:
            continue
        path = parts[1].strip().split(" ")[0]
        if not path.startswith("/"):
            continue
        if any(Path(path).name.startswith(prefix) for prefix in LINUX_SYSTEM_LIBS):
            continue
        deps.append(Path(path))
    return deps


def bundle_linux(src: Path, dest: Path, tools: list[str]) -> None:
    bin_dir, lib_dir = dest / "bin", dest / "lib"
    bin_dir.mkdir(parents=True, exist_ok=True)
    lib_dir.mkdir(parents=True, exist_ok=True)
    pending: list[Path] = []
    for tool in tools:
        target = bin_dir / tool
        shutil.copy2(src / tool, target)
        os.chmod(target, 0o755)
        run("patchelf", "--set-rpath", "$ORIGIN/../lib", str(target))
        pending += _linux_deps(src / tool)
    seen: set[str] = set()
    while pending:
        lib = pending.pop()
        if lib.name in seen:
            continue
        seen.add(lib.name)
        target = lib_dir / lib.name
        shutil.copy2(lib.resolve(), target)
        os.chmod(target, 0o755)
        run("patchelf", "--set-rpath", "$ORIGIN", str(target))
        pending += _linux_deps(lib)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    out = Path(argv[0])
    for spec in argv[1:]:
        engine, _, folder = spec.partition("=")
        src = Path(folder)
        dest = out / engine
        if sys.platform == "win32":
            bundle_windows(src, dest, TOOLS[engine])
        elif sys.platform == "darwin":
            bundle_macos(src, dest, TOOLS[engine])
        else:
            bundle_linux(src, dest, TOOLS[engine])
        print(f"bundled {engine} from {src} into {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
