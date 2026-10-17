#!/usr/bin/env python3
"""Collect a prebuilt llama.cpp bundle from a from-source sys-crate build.

The release CI runs a normal (from-source) ``cargo build -p llama-cpp-sys-2``
for a given target/variant, which produces the platform-specific ``bindings.rs``
and the static archives under the build script's ``OUT_DIR``. This script
harvests exactly the artifacts the ``prebuilt`` build.rs path links against and
lays them out as:

    <dest>/
      bindings.rs        platform-specific bindgen output
      manifest.json      metadata + per-file sha256
      lib/<archive>      static archives (libllama.a, libggml*.a, wrapper, ...)

Downstream consumers (zvec-grep) download this bundle, point
``ZVEC_LLAMA_PREBUILT_DIR`` at it, and build ``llama-cpp-sys-2`` with the
``prebuilt`` feature so no CMake/bindgen/cc toolchain is needed.

Only stdlib is used so it runs on every GitHub-hosted runner (incl. Windows).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

# Archive locations inside OUT_DIR, relative to the from-source build. The
# vendor tree (e.g. cpp-httplib) is intentionally excluded: it is not part of
# the link line emitted by build.rs and is already folded into llama-common.
_LIB_SUBDIRS = ("lib", "lib64", Path("build") / "common")
_WRAPPER_STEMS = ("llama_cpp_sys_2_common_wrapper",)
_EXCLUDE_PARTS = {"vendor"}


def _is_archive(path: Path) -> bool:
    return path.suffix in (".a", ".lib") and path.is_file()


def _find_archives(out_dir: Path) -> list[Path]:
    found: dict[str, Path] = {}

    def consider(path: Path) -> None:
        if any(part in _EXCLUDE_PARTS for part in path.relative_to(out_dir).parts):
            return
        # Keep the first occurrence; prefer shallower paths for determinism.
        found.setdefault(path.name, path)

    for sub in _LIB_SUBDIRS:
        d = out_dir / sub
        if d.is_dir():
            for path in sorted(d.rglob("*")):
                if _is_archive(path):
                    consider(path)

    # The cc-compiled wrapper archive lands at the OUT_DIR root.
    for stem in _WRAPPER_STEMS:
        for ext in (".a", ".lib"):
            candidate = out_dir / f"lib{stem}{ext}"
            if not candidate.is_file():
                candidate = out_dir / f"{stem}{ext}"
            if candidate.is_file():
                consider(candidate)

    return sorted(found.values(), key=lambda p: p.name)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", required=True, type=Path,
                    help="sys-crate build script OUT_DIR (contains bindings.rs)")
    ap.add_argument("--dest", required=True, type=Path, help="bundle output dir")
    ap.add_argument("--target", required=True, help="rust target triple")
    ap.add_argument("--variant", required=True, help="cpu | cuda | vulkan ...")
    ap.add_argument("--version", required=True, help="llama-cpp-rs version, e.g. 0.1.154")
    ap.add_argument("--llama-cpp-commit", default="", help="vendored llama.cpp commit")
    ap.add_argument("--profile", default="release")
    args = ap.parse_args()

    out_dir: Path = args.out_dir
    if not out_dir.is_dir():
        print(f"error: OUT_DIR not found: {out_dir}", file=sys.stderr)
        return 2

    bindings = out_dir / "bindings.rs"
    if not bindings.is_file():
        print(f"error: bindings.rs not found in {out_dir}", file=sys.stderr)
        return 2

    archives = _find_archives(out_dir)
    if not archives:
        print(f"error: no static archives found under {out_dir}", file=sys.stderr)
        return 2

    dest: Path = args.dest
    lib_dir = dest / "lib"
    if dest.exists():
        shutil.rmtree(dest)
    lib_dir.mkdir(parents=True)

    shutil.copy2(bindings, dest / "bindings.rs")

    files: dict[str, str] = {}
    lib_names: list[str] = []
    for arc in archives:
        shutil.copy2(arc, lib_dir / arc.name)
        files[f"lib/{arc.name}"] = _sha256(lib_dir / arc.name)
        stem = arc.name
        for prefix in ("lib",):
            if stem.startswith(prefix):
                stem = stem[len(prefix):]
        stem = stem.rsplit(".", 1)[0]
        lib_names.append(stem)
    files["bindings.rs"] = _sha256(dest / "bindings.rs")

    manifest = {
        "schema": 1,
        "version": args.version,
        "llama_cpp_commit": args.llama_cpp_commit,
        "target": args.target,
        "variant": args.variant,
        "profile": args.profile,
        "libs": sorted(lib_names),
        "sha256": files,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"collected {len(archives)} archives + bindings.rs -> {dest}")
    for name in sorted(lib_names):
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
