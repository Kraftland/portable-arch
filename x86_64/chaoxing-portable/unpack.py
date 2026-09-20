#!/usr/bin/env python3
"""Unpack a cx-code-encryption protected app.asar into plain source.

cxstudy (超星学习通 / PC版) ships its JavaScript as `.jscx` files that are
AES-128-ECB encrypted and only loadable through a Windows/macOS native addon.
This script produces an equivalent tree of plain `.js` files.

Layout rules (reverse-engineered, see NOTES.md):
  * Every asar entry carries a 2-byte header prefix; content starts at offset+2.
  * `.jscx` payload = base64 ciphertext, AES-128-ECB, empty IV, PKCS#7.
  * Per-file key:
        h1  = md5("<basename>_chenxi")
        h2  = md5("chaoxing_" + h1)
        key = h2[1:5] + "." + h1[7:10] + "*" + h2[12:19]

Usage: unpack_asar.py <app.asar> <output-dir>
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

COMPILED_EXT = ".jscx"
HEADER_PREFIX_LEN = 2


def key_for(basename: str) -> str:
    h1 = hashlib.md5((basename + "_chenxi").encode()).hexdigest()
    h2 = hashlib.md5(("chaoxing_" + h1).encode()).hexdigest()
    return h2[1:5] + "." + h1[7:10] + "*" + h2[12:19]


def decrypt(ciphertext: bytes, key: str) -> bytes:
    dec = Cipher(algorithms.AES(key.encode()), modes.ECB()).decryptor()
    return dec.update(ciphertext) + dec.finalize()


def strip_pkcs7(data: bytes) -> bytes:
    """The addon tolerates bad padding on short buffers, so only strip when valid."""
    if data and 1 <= data[-1] <= 16 and data.endswith(bytes([data[-1]]) * data[-1]):
        return data[: -data[-1]]
    return data


def walk(node: dict, prefix: str = ""):
    if "files" in node:
        for name, child in node["files"].items():
            yield from walk(child, prefix + "/" + name)
    elif "offset" in node:
        yield prefix, int(node["offset"]), int(node["size"])


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    asar_path, out_dir = sys.argv[1], sys.argv[2]

    with open(asar_path, "rb") as fh:
        raw = fh.read()

    header_size = int.from_bytes(raw[12:16], "little")
    header = json.loads(raw[16 : 16 + header_size])
    base = 16 + header_size

    n_dec = n_plain = n_fail = 0
    for entry, offset, size in walk(header):
        start = base + offset + HEADER_PREFIX_LEN
        body = raw[start : start + size]
        if not body:
            continue

        dest = os.path.join(out_dir, entry.lstrip("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        if entry.endswith(COMPILED_EXT):
            try:
                plain = strip_pkcs7(
                    decrypt(base64.b64decode(body), key_for(os.path.basename(entry)))
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL {entry}: {exc}", file=sys.stderr)
                n_fail += 1
                continue
            with open(dest[: -len(COMPILED_EXT)] + ".js", "wb") as fh:
                fh.write(plain)
            n_dec += 1
        else:
            with open(dest, "wb") as fh:
                fh.write(body)
            n_plain += 1

    print(f"decrypted={n_dec} plain={n_plain} failed={n_fail}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
