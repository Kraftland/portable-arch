#!/usr/bin/env python3
"""Patch a decrypted cxstudy tree so it runs under Linux system Electron.

Applies four independent fixes, all previously verified against the real app:

 1. cx-code-encryption -> Linux passthrough
    The package only assigns `compileExt` for win32/darwin, so on Linux the
    `.jscx` require hook never registers. Replace it (both copies) with a hook
    that resolves `foo.jscx` to the already-decrypted `foo.js`.

 2. @journeyapps/sqlcipher Linux binding
    Ship the official prebuilt napi-v6-linux-x64 binary and its OpenSSL 1.1
    dependency (Arch no longer provides libcrypto.so.1.1).

 3. agora-electron-sdk Linux stub
    Real package is Windows/macOS-only. Stub reports RTC as unsupported.

 4. rkcloud-conference-electron-sdk Linux stub
    Windows-only .dll SDK. Same graceful-degradation approach.

Usage: patch_app.py <app-root> <assets-dir>
"""
from __future__ import annotations

import os
import shutil
import sys

JSCX_HOOK = r'''"use strict";
/**
 * Linux passthrough replacement for cx-code-encryption.
 *
 * The original registers a `.jscx` require hook backed by a Windows/macOS-only
 * native addon; on Linux no addon is selected so the hook never registers.
 * Here we register the equivalent hook but load the already-decrypted `.js`
 * sibling instead — same module semantics, no native code.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.getDbKey = getDbKey;

const fs = require("fs");
const Module = require("module");

const COMPILED_EXTNAME = ".jscx";

function decryptedSibling(filename) {
    if (!filename.endsWith(COMPILED_EXTNAME)) return null;
    const candidate = filename.slice(0, -COMPILED_EXTNAME.length) + ".js";
    try {
        if (fs.existsSync(candidate)) return candidate;
    } catch (_) { /* fall through */ }
    return null;
}

const originalResolveFilename = Module._resolveFilename;
Module._resolveFilename = function (request, parent, isMain, options) {
    try {
        return originalResolveFilename.call(this, request, parent, isMain, options);
    } catch (err) {
        if (err && err.code === "MODULE_NOT_FOUND") {
            const swapped = decryptedSibling(request);
            if (swapped) {
                return originalResolveFilename.call(this, swapped, parent, isMain, options);
            }
        }
        throw err;
    }
};

Module._extensions[COMPILED_EXTNAME] = function (fileModule, filename) {
    const swapped = decryptedSibling(filename);
    if (swapped) {
        fileModule.exports = Module._load(swapped, fileModule, false);
        return;
    }
    fileModule._compile(fs.readFileSync(filename, "utf8"), filename);
};

// The Windows addon returns the SQLCipher key for a database name. Without the
// addon this cannot be reproduced; a falsey key means "no encryption".
function getDbKey(_dbname) {
    return "";
}

for (const extra of ["./Jscx", "./CompileUtil"]) {
    try { require(extra); } catch (_) { /* optional */ }
}
'''

JSCX_ENTRY = '''"use strict";
function Jscx() {
    console.log("Jscx");
}
'''

COMPILEUTIL_ENTRY = '''"use strict";
// Linux stub: the real HTTP helpers live in the Windows/macOS native addon.
Object.defineProperty(exports, "__esModule", { value: true });
exports.getDbKey = getDbKey;
function getDbKey(_dbname) {
    return "";
}
'''

AGORA_PKG = {
    "name": "agora-electron-sdk",
    "version": "0.0.0-linux-stub",
    "description": "Linux stub: the real SDK ships Windows/macOS native libraries only.",
    "main": "index.js",
    "private": True,
}

RK_PKG = {
    "name": "rkcloud-conference-electron-sdk",
    "version": "0.0.0-linux-stub",
    "description": "Linux stub: the real SDK ships Windows-only native libraries.",
    "main": "index.js",
    "private": True,
}

COMMON_ENUM = '''"use strict";
// Re-export the enum set (real package splits these into a subpath module).
module.exports = require("./index.js");
'''


def replace_encryption_hook(app_root: str) -> int:
    """Rewrite both copies of cx-code-encryption to the Linux passthrough."""
    targets = [
        "node_modules/cx-code-encryption",
        "node_modules/@cx/cxcore/node_modules/cx-code-encryption",
    ]
    patched = 0
    for rel in targets:
        dist = os.path.join(app_root, rel, "dist")
        if not os.path.isdir(dist):
            continue
        with open(os.path.join(dist, "index.js"), "w") as fh:
            fh.write(JSCX_HOOK)
        with open(os.path.join(dist, "Jscx.js"), "w") as fh:
            fh.write(JSCX_ENTRY)
        with open(os.path.join(dist, "CompileUtil.js"), "w") as fh:
            fh.write(COMPILEUTIL_ENTRY)
        patched += 1
    return patched


def install_sqlcipher_binding(app_root: str, assets: str) -> bool:
    src = os.path.join(assets, "sqlcipher")
    if not os.path.isdir(src):
        return False
    dest = os.path.join(
        app_root,
        "node_modules/@journeyapps/sqlcipher/lib/binding/napi-v6-linux-x64",
    )
    os.makedirs(dest, exist_ok=True)
    shutil.copy2(os.path.join(src, "node_sqlite3.node"), dest)
    return True


def write_json(path: str, payload: dict) -> None:
    import json

    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")


def install_stubs(app_root: str, assets: str) -> int:
    nm = os.path.join(app_root, "node_modules")
    n = 0

    agora_src = os.path.join(assets, "agora-electron-sdk.js")
    agora_dst = os.path.join(nm, "agora-electron-sdk")
    if os.path.isfile(agora_src):
        os.makedirs(agora_dst, exist_ok=True)
        shutil.copy2(agora_src, os.path.join(agora_dst, "index.js"))
        write_json(os.path.join(agora_dst, "package.json"), AGORA_PKG)
        n += 1

    rk_src = os.path.join(assets, "rkcloud-conference-electron-sdk.js")
    rk_dst = os.path.join(nm, "rkcloud-conference-electron-sdk")
    if os.path.isfile(rk_src):
        os.makedirs(rk_dst, exist_ok=True)
        shutil.copy2(rk_src, os.path.join(rk_dst, "index.js"))
        with open(os.path.join(rk_dst, "common_enum.js"), "w") as fh:
            fh.write(COMMON_ENUM)
        write_json(os.path.join(rk_dst, "package.json"), RK_PKG)
        n += 1

    return n


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    app_root, assets = sys.argv[1], sys.argv[2]

    hooks = replace_encryption_hook(app_root)
    sqlc = install_sqlcipher_binding(app_root, assets)
    stubs = install_stubs(app_root, assets)

    print(f"encryption hooks patched: {hooks}")
    print(f"sqlcipher binding installed: {sqlc}")
    print(f"RTC stubs installed: {stubs}")

    if hooks == 0:
        print("ERROR: no cx-code-encryption copy found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
