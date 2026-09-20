#!/usr/bin/env python3
"""Patch a decrypted cxstudy tree so it runs under Linux system Electron.

Applies five independent fixes, all previously verified against the real app:

 1. cx-code-encryption -> Linux passthrough
    The package only assigns `compileExt` for win32/darwin, so on Linux the
    `.jscx` require hook never registers. Replace it (both copies) with a hook
    that resolves `foo.jscx` to the already-decrypted `foo.js`.

 2. @journeyapps/sqlcipher Linux binding
    The Windows binding shipped in app.asar cannot load. Fetch the official
    prebuilt napi-v6-linux-x64 binary and drop it in.

 3. agora-electron-sdk Linux stub
    Real package is Windows/macOS-only. Stub reports RTC as unsupported.

 4. rkcloud-conference-electron-sdk Linux stub
    Windows-only .dll SDK. Same graceful-degradation approach.

 5. Windows-only device fingerprinting
    Several call sites assume "not darwin" means Windows and shell out to
    `wmic`. Guard them, and teach the machine-id helper to read Linux sources.

All stub sources are embedded below, so this script is self-contained and needs
no companion `assets/` directory.

Usage: patch.py <app-root>
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request

# --------------------------------------------------------------------------
# SQLCipher: official prebuilt Linux binding.
# node-pre-gyp resolves `napi-v{napi_build_version}-{platform}-{arch}` under
# lib/binding, so this is exactly the path it looks for on linux/x64.
# --------------------------------------------------------------------------
SQLCIPHER_URL = (
    "https://journeyapps-node-binary.s3.amazonaws.com/"
    "@journeyapps/sqlcipher/v5.3.1/napi-v6-linux-x64.tar.gz"
)
SQLCIPHER_SHA256 = ""  # optional: fill to hard-pin; empty disables the check

# --------------------------------------------------------------------------
# Embedded stub sources
# --------------------------------------------------------------------------
JSCX_HOOK = r'''"use strict";
/**
 * Linux passthrough replacement for cx-code-encryption.
 *
 * The original registers a `.jscx` require hook backed by a Windows/macOS-only
 * native addon; on Linux no addon is selected so the hook never registers.
 * Here we register the equivalent hook but load the already-decrypted `.js`
 * sibling instead - same module semantics, no native code.
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
Object.defineProperty(exports, "__esModule", { value: true });
exports.getDbKey = getDbKey;
function getDbKey(_dbname) {
    return "";
}
'''

AGORA_STUB = r'''"use strict";
/**
 * Linux stub for `agora-electron-sdk` (Windows/macOS-only native SDK).
 *
 * Only the live-classroom / RTC path touches this. The real package exposes a
 * default factory plus enums read at module scope:
 *   default():  -> NodeRtcEngine
 *   AgoraEnv:   -> { AgoraRendererManager, AgoraElectronBridge }
 *   RenderModeType / VideoSourceType / ScreenCaptureSourceType / ClientRoleType
 *
 * Every RTC feature reports unsupported so callers take their own
 * "not supported" branch instead of crashing on a missing module.
 */
Object.defineProperty(exports, "__esModule", { value: true });

function noopProxy(name) {
    const fn = function () { return fn; };
    return new Proxy(fn, {
        get(_t, prop) {
            if (prop === "then") return undefined;
            if (prop === Symbol.toPrimitive) return () => name;
            if (prop === "toString") return () => `[stub:${name}]`;
            return noopProxy(`${name}.${String(prop)}`);
        },
        apply() { return noopProxy(name); },
    });
}

class StubRtcEngine {
    constructor() { this.__isStub = true; }
    initialize() { return -1; }
    release() { return 0; }
    registerEventHandler() {}
    unregisterEventHandler() {}
    enableVideo() { return -1; }
    enableAudio() { return -1; }
    disableVideo() {}
    disableAudio() {}
    joinChannel() { return -1; }
    leaveChannel() { return 0; }
    muteLocalAudioStream() { return -1; }
    muteLocalVideoStream() { return -1; }
    setClientRole() { return -1; }
    startPreview() { return -1; }
    stopPreview() {}
    enumerateDevices() { return []; }
    getScreenWindowsInfo() { return []; }
    getScreenDisplaysInfo() { return []; }
    startScreenCapture() { return -1; }
    stopScreenCapture() { return 0; }
    queryInterface() { return this; }
}

const engineProxy = new Proxy(new StubRtcEngine(), {
    get(target, prop) {
        if (prop in target) return target[prop];
        if (typeof prop === "symbol") return undefined;
        return () => -1;
    },
});

function createAgoraRtcEngine() {
    console.warn("[agora-stub] RTC unavailable on this platform");
    return engineProxy;
}

exports.default = createAgoraRtcEngine;

exports.AgoraEnv = {
    AgoraRendererManager: { renderers: new Map() },
    AgoraElectronBridge: {
        GetVideoFrame: () => null,
        EnableVideoFrameCache: () => {},
        DisableVideoFrameCache: () => {},
    },
};

exports.RenderModeType = { RENDER_MODE_HIDDEN: 1, RENDER_MODE_FIT: 2, RENDER_MODE_ADAPTIVE: 3 };
exports.VideoSourceType = { VIDEO_SOURCE_UNKNOWN: 0, VIDEO_SOURCE_CAMERA: 1, VIDEO_SOURCE_SCREEN: 2 };
exports.ScreenCaptureSourceType = { SCREEN_CAPTURE_SOURCE_WINDOW: 0, SCREEN_CAPTURE_SOURCE_SCREEN: 1 };
exports.ClientRoleType = { CLIENT_ROLE_BROADCASTER: 1, CLIENT_ROLE_AUDIENCE: 2 };

module.exports = new Proxy(module.exports, {
    get(target, prop) {
        if (prop in target) return target[prop];
        if (typeof prop === "symbol") return undefined;
        return noopProxy(String(prop));
    },
});
'''

RK_STUB = r'''"use strict";
/**
 * Linux stub for `rkcloud-conference-electron-sdk` (Windows-only .dll SDK).
 *
 * Used only by the live-classroom path:
 *   rkExtend/rk_clound_conference_extend.js:  new RkConferenceEngine()
 * Also exports free functions and type holders. Enums are declared at their real
 * ordinal positions where known so `==` comparisons resolve to a defined branch.
 */
Object.defineProperty(exports, "__esModule", { value: true });

class RkConferenceEngine {
    constructor() {
        this.__isStub = true;
        this.roomId = "";
        this.userId = "";
    }
    init() { return -1; }
    unInit() {}
    joinRoom() { return -1; }
    leaveRoom() { return -1; }
    setVideoOption() { return -1; }
    startScreenShare() { return -1; }
    stopScreenShare() { return -1; }
    getRoomInfo() { return null; }
    getUserList() { return []; }
    getLocalUser() { return null; }
    getStats() { return null; }
    sendMessage() { return -1; }
}
exports.RkConferenceEngine = RkConferenceEngine;

exports.logInfo = (...a) => console.log("[rk-stub]", ...a);
exports.logWarn = (...a) => console.warn("[rk-stub]", ...a);
exports.logError = (...a) => console.error("[rk-stub]", ...a);
exports.enableDevTools = () => {};
exports.messageResult = () => ({ code: -1, message: "rkcloud stub: unsupported on linux" });

class VideoOption { constructor(o = {}) { Object.assign(this, o); } }
class MsgData { constructor(o = {}) { Object.assign(this, o); } }
class RoomInfo { constructor(o = {}) { Object.assign(this, o); } }
class RtcConnection { constructor(o = {}) { Object.assign(this, o); } }
class Stats { constructor(o = {}) { Object.assign(this, o); } }
class User { constructor(o = {}) { Object.assign(this, o); } }

exports.VideoOption = VideoOption;
exports.MsgData = MsgData;
exports.RoomInfo = RoomInfo;
exports.RtcConnection = RtcConnection;
exports.Stats = Stats;
exports.User = User;

exports.ScreenDeviceType = { SCREEN: 1, WINDOW: 2 };
exports.UserDeviceState = { PLUGGED: 1, UNPLUGGED: 0 };
exports.RoomState = { INIT: 0, STARTED: 1, INTERRUPTED: 2, STOPED: 3, ENDED: 4 };
exports.AudioDeviceType = { PLAYOUT: 0, RECORDING: 1 };
exports.VideoCaptureDeviceType = { CAMERA: 0, SCREEN: 1 };
exports.MediaDeviceState = { PLUGGED: 1, UNPLUGGED: 0 };
exports.VideoState = { STARTED: 1, STOPED: 0 };
exports.ErrorCode = { SUCCESS: 0, FAILED: -1 };
exports.Resolution = { LOW: 0, MEDIUM: 1, HIGH: 2 };
exports.FPS = { FPS_15: 15, FPS_24: 24, FPS_30: 30 };
exports.Bitrate = { LOW: 0, MEDIUM: 1, HIGH: 2 };
exports.VideoQuality = { LOW: 0, MEDIUM: 1, HIGH: 2 };

module.exports = new Proxy(module.exports, {
    get(target, prop) {
        if (prop in target) return target[prop];
        if (typeof prop === "symbol") return undefined;
        return {};
    },
});
'''

COMMON_ENUM = '''"use strict";
module.exports = require("./index.js");
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

# --------------------------------------------------------------------------
# Fingerprint patching (pattern A: unguarded `wmic` in a non-win32 branch)
# --------------------------------------------------------------------------
WMIC_ELSE_RE = re.compile(
    r"(\n(\s*)else\s*\{\s*\n)"
    r"(\s*)((?:\w+\.)*\w+)\s*=\s*"
    r"((?:\w+\.)*\w+)\.execSync\("
    r"(\"wmic[^\"]*\"|`wmic[^`]*`)"
    r"(\s*,\s*\{[^}]*\}\s*)?\)\s*;",
    re.MULTILINE,
)

# Pattern C: Linux sources to prepend to executeCommandWithFallback chains.
DMI_UUID = "cat /sys/class/dmi/id/product_uuid 2>/dev/null || true"
DMI_SERIAL = "cat /sys/class/dmi/id/product_serial 2>/dev/null || true"
DISK_SERIAL = (
    "lsblk -ndo SERIAL /dev/$(lsblk -ndo PKNAME $(findmnt -no SOURCE /) "
    "2>/dev/null | head -1) 2>/dev/null || true"
)
MACHINE_ID = (
    "cat /etc/machine-id 2>/dev/null || "
    "cat /var/lib/dbus/machine-id 2>/dev/null || true"
)
PRODUCT_NAME = "cat /sys/class/dmi/id/product_name 2>/dev/null || true"

MACHINEID_LINUX_ADDITIONS = {
    "getProductUuid": [DMI_UUID, MACHINE_ID, PRODUCT_NAME],
    "getDiskSerialnumber": [DMI_SERIAL, DISK_SERIAL, MACHINE_ID],
}


# --------------------------------------------------------------------------
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
        for name, body in (
            ("index.js", JSCX_HOOK),
            ("Jscx.js", JSCX_ENTRY),
            ("CompileUtil.js", COMPILEUTIL_ENTRY),
        ):
            with open(os.path.join(dist, name), "w") as fh:
                fh.write(body)
        patched += 1
    return patched


def install_sqlcipher_binding(app_root: str) -> bool:
    """Fetch and install the official prebuilt Linux sqlcipher binding."""
    import io
    import tarfile
    import tempfile

    dest_dir = os.path.join(
        app_root,
        "node_modules/@journeyapps/sqlcipher/lib/binding/napi-v6-linux-x64",
    )
    dest = os.path.join(dest_dir, "node_sqlite3.node")

    data = None
    try:
        with urllib.request.urlopen(SQLCIPHER_URL, timeout=120) as resp:
            data = resp.read()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not download sqlcipher binding: {exc}", file=sys.stderr)
        return False

    if SQLCIPHER_SHA256:
        got = hashlib.sha256(data).hexdigest()
        if got != SQLCIPHER_SHA256:
            print(
                f"ERROR: sqlcipher tarball checksum mismatch\n"
                f"  expected {SQLCIPHER_SHA256}\n  got      {got}",
                file=sys.stderr,
            )
            return False

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        member = next(
            (m for m in tf.getmembers() if m.name.endswith("node_sqlite3.node")),
            None,
        )
        if member is None:
            print("ERROR: node_sqlite3.node not found in archive", file=sys.stderr)
            return False
        extracted = tf.extractfile(member)
        if extracted is None:
            return False
        blob = extracted.read()

    if blob[:4] != b"\x7fELF":
        print(f"ERROR: fetched binding is not a Linux ELF", file=sys.stderr)
        return False

    os.makedirs(dest_dir, exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(blob)
    os.chmod(dest, 0o755)
    return True


def write_json(path: str, payload: dict) -> None:
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")


def install_stubs(app_root: str) -> int:
    nm = os.path.join(app_root, "node_modules")
    n = 0

    agora_dst = os.path.join(nm, "agora-electron-sdk")
    os.makedirs(agora_dst, exist_ok=True)
    with open(os.path.join(agora_dst, "index.js"), "w") as fh:
        fh.write(AGORA_STUB)
    write_json(os.path.join(agora_dst, "package.json"), AGORA_PKG)
    n += 1

    rk_dst = os.path.join(nm, "rkcloud-conference-electron-sdk")
    os.makedirs(rk_dst, exist_ok=True)
    with open(os.path.join(rk_dst, "index.js"), "w") as fh:
        fh.write(RK_STUB)
    with open(os.path.join(rk_dst, "common_enum.js"), "w") as fh:
        fh.write(COMMON_ENUM)
    write_json(os.path.join(rk_dst, "package.json"), RK_PKG)
    n += 1

    return n


def guard_wmic(src: str) -> tuple[str, int]:
    """Wrap single-statement `wmic` execSync calls in a win32 guard.

    Handles the common one-liner form:  x = cp.execSync(`wmic ...`, {...});
    """
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        count += 1
        lead, indent = m.group(1), m.group(3)
        assign, proc = m.group(4), m.group(5)
        cmd, opts = m.group(6), m.group(7) or ""
        return (
            f'{lead}{indent}if (process.platform == "win32") {{\n'
            f"{indent}    {assign} = {proc}.execSync({cmd}{opts});\n"
            f"{indent}}}"
        )

    return WMIC_ELSE_RE.sub(repl, src), count


# A `wmic` call as a bare statement:
#     let x = (0, cp.execSync)(`wmic ...`, {...});
#     x = cp.execSync("wmic ...", {...});
# The whole statement is matched (including the options object) so the
# replacement consumes every line it touches and cannot leave dangling code.
WMIC_STMT_RE = re.compile(
    r"^([ \t]*)((?:let|const|var)\s+\w+\s*=\s*)?"
    r"(?:\((?:0),\s*)?((?:[\w.]+\.)*\w+)\.execSync\)?\("
    r"\s*(`wmic[^`]*`|\"wmic[^\"]*\")"
    r"(\s*,\s*\{.*?\}\s*)?\)\s*;",
    re.MULTILINE | re.DOTALL,
)


def guard_wmic_statements(src: str) -> tuple[str, int]:
    """Neutralise bare `wmic` statements that have no win32 guard.

    Each call is wrapped individually rather than wrapping the enclosing block,
    so surrounding try/catch and falsy checks keep working: on Linux the call
    is skipped, the variable stays undefined, and the existing `if (!x)` guards
    take their fallback path.

    Matches are replaced whole (options object included) so no dangling lines
    are left behind. A match that already sits inside a win32 guard is left
    untouched.
    """
    count = 0
    out: list[str] = []
    pos = 0

    for m in WMIC_STMT_RE.finditer(src):
        indent = m.group(1)
        decl = m.group(2) or ""
        proc = m.group(3)
        cmd = m.group(4)
        opts = re.sub(r"\s+", " ", m.group(5) or "").strip()

        # Skip anything already guarded: look at the most recent preceding
        # non-empty line for a win32 platform check.
        preceding = [ln for ln in src[: m.start()].split("\n") if ln.strip()]
        if preceding and 'process.platform == "win32"' in preceding[-1]:
            continue

        call = f"({proc}.execSync)({cmd}{opts});"
        out.append(src[pos : m.start()])
        out.append(
            f'{indent}if (process.platform == "win32") {{\n'
            f"{indent}    {decl}{call}\n"
            f"{indent}}}"
        )
        pos = m.end()
        count += 1

    out.append(src[pos:])
    return "".join(out), count


def add_linux_fallbacks(src: str) -> tuple[str, int]:
    count = 0
    for func_name, cmds in MACHINEID_LINUX_ADDITIONS.items():
        pattern = re.compile(
            rf"(function\s+{func_name}\s*\(\)\s*\{{\s*return\s+"
            r"executeCommandWithFallback\(\s*\[)",
            re.MULTILINE,
        )
        m = pattern.search(src)
        if not m:
            continue
        if cmds[0] in src[m.start() : m.start() + 900]:
            continue  # already patched
        addition = "".join(f'\n        "{c}",' for c in cmds)
        src = src[: m.end()] + addition + src[m.end() :]
        count += 1
    return src, count


def patch_fingerprints(app_root: str) -> tuple[int, int, int]:
    total_a = total_b = total_c = 0

    for base in ("electron", "module", "dist", "node_modules/@cx"):
        root = os.path.join(app_root, base)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(".js"):
                    continue
                p = os.path.join(dirpath, name)
                try:
                    with open(p) as fh:
                        src = fh.read()
                except OSError:
                    continue
                if "wmic" not in src:
                    continue
                new, n = guard_wmic(src)
                new, n2 = guard_wmic_statements(new)
                n += n2
                if n:
                    with open(p, "w") as fh:
                        fh.write(new)
                    print(f"  guarded {n} wmic call(s): {os.path.relpath(p, app_root)}")
                    total_a += n
                    total_b += n2

    mi = os.path.join(app_root, "node_modules/@cx/cxcore/dist/util/MachineIdUtil.js")
    if os.path.isfile(mi):
        try:
            with open(mi) as fh:
                src = fh.read()
            new, n = add_linux_fallbacks(src)
            if n:
                with open(mi, "w") as fh:
                    fh.write(new)
                print(f"  added {n} Linux machine-id fallback(s)")
                total_c = n
        except OSError:
            pass

    return total_a, total_b, total_c


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    app_root = sys.argv[1]

    hooks = replace_encryption_hook(app_root)
    sqlc = install_sqlcipher_binding(app_root)
    stubs = install_stubs(app_root)
    wmic_a, wmic_b, wmic_c = patch_fingerprints(app_root)

    print(f"encryption hooks patched: {hooks}")
    print(f"sqlcipher binding installed: {sqlc}")
    print(f"RTC stubs installed: {stubs}")
    print(f"wmic else-branch guards added: {wmic_a - wmic_b}")
    print(f"wmic bare-statement guards added: {wmic_b}")
    print(f"linux machine-id fallbacks added: {wmic_c}")

    # Fail hard: a tree without these produces a package that dies on first
    # launch with a confusing MODULE_NOT_FOUND, or silently reports as a new
    # device to the server on every run.
    rc = 0
    if hooks == 0:
        print("ERROR: no cx-code-encryption copy found", file=sys.stderr)
        rc = 1
    if not sqlc:
        print("ERROR: sqlcipher linux binding not installed", file=sys.stderr)
        rc = 1
    if stubs < 2:
        print(f"ERROR: expected 2 RTC stubs, installed {stubs}", file=sys.stderr)
        rc = 1
    if wmic_c < 2:
        print(
            f"ERROR: expected 2 linux machine-id fallbacks, added {wmic_c}",
            file=sys.stderr,
        )
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
