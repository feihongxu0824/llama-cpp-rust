#!/usr/bin/env python3
"""Fetch a prebuilt llama.cpp bundle from GitHub Releases for the current target.

Used by downstream CI (e.g. zvec-grep) before `cargo build`. It downloads the
bundle matching a target triple + variant, verifies its sha256, extracts it, and
prints the directory to point `ZVEC_LLAMA_PREBUILT_DIR` at.

Exit codes let CI implement the source-build fallback described in PREBUILT.md:
  0  bundle fetched and verified (path printed on stdout)
  3  no bundle published for this target/variant (caller should build from source)
  4  download/checksum/extraction failure (hard error)

Only stdlib is used, so it runs on every CI runner (incl. Windows).

Example:
  python3 scripts/fetch_prebuilt.py \
      --repo zvec-ai/llama-cpp-rust \
      --tag prebuilt-0.1.154-3 \
      --target aarch64-apple-darwin --variant cpu --version 0.1.154 \
      --dest target/llama-prebuilt
  # then: export ZVEC_LLAMA_PREBUILT_DIR=<printed path>
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

NO_BUNDLE = 3
FAILURE = 4


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _download(url: str, token: str | None) -> bytes | None:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "zvec-llama-prebuilt-fetcher")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    except urllib.error.URLError:
        return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default="zvec-ai/llama-cpp-rust",
                    help="GitHub repo that hosts the release (owner/name)")
    ap.add_argument("--tag", required=True, help="release tag, e.g. prebuilt-0.1.154-3")
    ap.add_argument("--target", required=True, help="rust target triple")
    ap.add_argument("--variant", default="cpu", help="cpu | cuda | vulkan")
    ap.add_argument("--version", required=True, help="bundle version, e.g. 0.1.154")
    ap.add_argument("--dest", required=True, type=Path,
                    help="directory to extract the bundle into")
    ap.add_argument("--token-env", default="GITHUB_TOKEN",
                    help="env var holding an optional token for private releases")
    args = ap.parse_args()

    token = os.environ.get(args.token_env) or None
    bundle = f"llama-prebuilt-{args.target}-{args.variant}-{args.version}"
    base = f"https://github.com/{args.repo}/releases/download/{args.tag}"
    asset_url = f"{base}/{bundle}.tar.gz"
    checksum_url = f"{base}/{bundle}.tar.gz.sha256"

    _eprint(f"fetching {asset_url}")
    try:
        archive = _download(asset_url, token)
    except Exception as exc:  # noqa: BLE001 - surface any network error as failure
        _eprint(f"error: download failed: {exc}")
        return FAILURE
    if archive is None:
        _eprint(f"no prebuilt bundle for {args.target}/{args.variant} at {args.tag}; "
                "fall back to a from-source build")
        return NO_BUNDLE

    # Verify the checksum when the sidecar is published.
    try:
        checksum_asset = _download(checksum_url, token)
    except Exception:  # noqa: BLE001
        checksum_asset = None
    if checksum_asset:
        expected = checksum_asset.decode().split()[0].strip().lower()
        actual = _sha256(archive).lower()
        if expected != actual:
            _eprint(f"error: sha256 mismatch (expected {expected}, got {actual})")
            return FAILURE
        _eprint("sha256 verified")
    else:
        _eprint("warning: no .sha256 sidecar found; skipping verification")

    dest: Path = args.dest
    bundle_dir = dest / bundle
    if bundle_dir.exists():
        import shutil
        shutil.rmtree(bundle_dir)
    dest.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
            # Guard against path traversal in the archive.
            for member in tf.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    _eprint(f"error: unsafe path in archive: {member.name}")
                    return FAILURE
            tf.extractall(dest)
    except (tarfile.TarError, OSError) as exc:
        _eprint(f"error: extraction failed: {exc}")
        return FAILURE

    if not (bundle_dir / "bindings.rs").is_file() or not (bundle_dir / "lib").is_dir():
        _eprint(f"error: extracted bundle is malformed: {bundle_dir}")
        return FAILURE

    print(bundle_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
