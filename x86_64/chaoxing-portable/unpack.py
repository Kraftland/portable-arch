#!/usr/bin/env python3
"""Unpack a cx-code-encryption protected app.asar into plain source.

cxstudy (超星学习通 / PC版) ships most of its JavaScript as `.jscx` files that
are AES-128-ECB encrypted and normally loaded through the
`cx-code-encryption` native addon.  This script turns them into ordinary `.js`
files so the tree can be patched and run by a system Electron.

The previous revision of this script assumed every asar entry starts with a
two-byte prefix and added `offset + 2`.  That is wrong: on 1.4.5 the data
area is contiguous and entries start exactly at `offset`.  The same
assumption also made the script copy stale `.jscx` duplicates over their
plain `.js` siblings.  This revision uses the real offset and keeps the plain
file when both exist.

Layout rules (reverse-engineered):
  * asar header: 16-byte preamble, then the JSON directory; the length word
    immediately before the JSON is the JSON length.  The data area starts
    right after it.
  * `.jscx` payload = base64 ciphertext, AES-128-ECB, empty IV, PKCS#7.
  * Per-file key:
        h1  = md5("<basename>_chenxi")
        h2  = md5("chaoxing_" + h1)
        key = h2[1:5] + "." + h1[7:10] + "*" + h2[12:19]
  * Some releases prepend up to 7 bytes of alignment padding to the base64
    text; the correct shift is found by requiring valid PKCS#7 padding *and*
    valid UTF-8 plaintext.

Usage:
    unpack.py <app.asar> <output-dir> [--unpacked <app.asar.unpacked>]

If --unpacked is omitted, a sibling `<app.asar>.unpacked` directory is merged
automatically when present.  Unpacked files are written over the asar tree,
which is what Electron does at runtime.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import struct
import sys
from typing import Iterator

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

COMPILED_EXT = ".jscx"
MAX_SHIFT = 8
HEADER_SCAN_LIMIT = 1 << 20  # the directory is always at the front of the file

# Plain-JS marker used only when a `.jscx` file turns out not to be encrypted.
JS_PREFIXES = (
    b'"use strict"',
    b"'use strict'",
    b"//",
    b"/*",
    b"var ",
    b"let ",
    b"const ",
    b"function ",
    b"exports.",
    b"module.exports",
    b"import ",
    b"(()=>",
    b"!function",
    b"(function",
)


class AsarError(Exception):
    pass


def key_for(basename: str) -> str:
    h1 = hashlib.md5((basename + "_chenxi").encode()).hexdigest()
    h2 = hashlib.md5(("chaoxing_" + h1).encode()).hexdigest()
    return h2[1:5] + "." + h1[7:10] + "*" + h2[12:19]


def strip_pkcs7(data: bytes) -> bytes | None:
    """Return data without valid PKCS#7 padding, otherwise None.

    Being strict here is what makes padding-shift detection reliable: random
    ciphertext only passes this check with probability ~1/256.
    """
    if not data:
        return None
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return None


def u32(data: bytes) -> int:
    return struct.unpack("<I", data)[0]


def load_header(raw: bytes) -> tuple[dict, int]:
    """Return (directory, data_offset).

    The canonical layout is:
        u32 4 | u32 header_size | u32 string_size | u32 string_size | JSON
    but older/newer packers have used slightly different preambles.  Locate the
    JSON instead of trusting fixed offsets, then take the length word directly
    before it.
    """
    candidates: list[int] = []
    found = raw.find(b'{"files"', 0, HEADER_SCAN_LIMIT)
    if found >= 4:
        candidates.append(found)
    for off in (16, 12, 8):
        if raw[off : off + 1] == b"{":
            candidates.append(off)

    for start in dict.fromkeys(candidates):
        if start < 4:
            continue
        length = u32(raw[start - 4 : start])
        end = start + length
        if length == 0 or end > len(raw):
            continue
        try:
            header = json.loads(raw[start:end])
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(header, dict) and isinstance(header.get("files"), dict):
            return header, end

    raise AsarError("not an asar archive (no valid JSON directory found)")


def walk(node: dict, prefix: str = "") -> Iterator[tuple[str, int, int]]:
    if "files" in node:
        for name, child in node["files"].items():
            yield from walk(child, prefix + "/" + name)
    elif "offset" in node:
        yield prefix, int(node["offset"]), int(node["size"])


def safe_dest(root: str, entry: str) -> str:
    """Join an asar entry onto root, refusing anything that escapes it."""
    rel = entry.replace("\\", "/").lstrip("/")
    dest = os.path.abspath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    if os.path.commonpath((root_abs, dest)) != root_abs:
        raise AsarError(f"unsafe path in asar: {entry!r}")
    return dest


def looks_like_js(data: bytes) -> bool:
    head = data.lstrip()[:64]
    if not head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return any(head.startswith(prefix) for prefix in JS_PREFIXES)


def decrypt_body(body: bytes, basename: str) -> bytes | None:
    """Decrypt one `.jscx` payload, tolerating 0-7 bytes of leading padding."""
    key = key_for(basename).encode()

    for shift in range(MAX_SHIFT):
        chunk = body[shift:]
        if not chunk:
            continue
        try:
            ciphertext = base64.b64decode(chunk, validate=False)
        except (ValueError, TypeError):
            continue
        if not ciphertext or len(ciphertext) % 16:
            continue
        try:
            decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            plain = decryptor.update(ciphertext) + decryptor.finalize()
        except ValueError:
            continue
        plain = strip_pkcs7(plain)
        if plain is None:
            continue
        try:
            text = plain.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            continue
        return plain

    return None


def merge_unpacked(src: str, dst: str) -> int:
    """Overlay an app.asar.unpacked tree onto the extracted tree.

    Symlinks are skipped: an NSIS payload should not contain any, and following
    one could copy arbitrary host files into the package.
    """
    count = 0
    for dirpath, _dirnames, filenames in os.walk(src, followlinks=False):
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else safe_dest(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in filenames:
            source = os.path.join(dirpath, name)
            if os.path.islink(source):
                print(f"  skip symlink: {os.path.relpath(source, src)}", file=sys.stderr)
                continue
            target = os.path.join(target_dir, name)
            shutil.copy2(source, target)
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asar", help="path to app.asar")
    parser.add_argument("out_dir", help="directory to write the unpacked tree to")
    parser.add_argument(
        "--unpacked",
        help="path to app.asar.unpacked (default: <asar>.unpacked if it exists)",
    )
    args = parser.parse_args()

    with open(args.asar, "rb") as fh:
        raw = fh.read()

    header, base = load_header(raw)
    os.makedirs(args.out_dir, exist_ok=True)

    decrypted = plain = shadowed = merged = failed = 0
    seen: set[str] = set()
    errors: list[str] = []

    entries = list(walk(header))
    # A `.js` sibling wins at runtime (Node resolves it before the registered
    # `.jscx` extension), so a same-named `.jscx` is a stale duplicate.  Keep
    # the plain file and do not let the duplicate collide with it.
    plain_paths = {
        os.path.normcase(safe_dest(args.out_dir, entry))
        for entry, _offset, _size in entries
        if not entry.endswith(COMPILED_EXT)
    }

    for entry, offset, size in entries:
        if size < 0 or offset < 0:
            errors.append(f"{entry}: negative offset/size")
            continue
        start = base + offset
        end = start + size
        if start < base or end > len(raw):
            errors.append(f"{entry}: data outside the archive")
            continue
        body = raw[start:end]
        dest = safe_dest(args.out_dir, entry)

        if entry.endswith(COMPILED_EXT):
            if os.path.normcase(dest[: -len(COMPILED_EXT)] + ".js") in plain_paths:
                shadowed += 1
                continue
            payload = decrypt_body(body, os.path.basename(entry))
            if payload is None:
                # Some releases have shipped plain JS under a .jscx name.
                if looks_like_js(body):
                    print(f"  warning: {entry} is not encrypted; copying as-is", file=sys.stderr)
                    payload = body
                else:
                    errors.append(f"{entry}: decryption failed")
                    failed += 1
                    continue
            dest = dest[: -len(COMPILED_EXT)] + ".js"
            decrypted += 1
        else:
            payload = body
            plain += 1

        key = os.path.normcase(dest)
        if key in seen:
            errors.append(f"{entry}: duplicate output path {dest}")
            continue
        seen.add(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(payload)

    unpacked = args.unpacked
    if unpacked is None and os.path.isdir(args.asar + ".unpacked"):
        unpacked = args.asar + ".unpacked"
    if unpacked:
        if os.path.isdir(unpacked):
            merged = merge_unpacked(unpacked, args.out_dir)
        else:
            print(f"warning: unpacked directory not found: {unpacked}", file=sys.stderr)

    print(
        f"decrypted={decrypted} plain={plain} shadowed={shadowed} "
        f"merged_unpacked={merged} failed={failed}"
    )
    for error in errors:
        print(f"  ERROR {error}", file=sys.stderr)
    if errors:
        print(f"unpack failed with {len(errors)} error(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AsarError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
