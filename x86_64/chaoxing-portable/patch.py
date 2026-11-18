#!/usr/bin/env python3
"""Make a decrypted cxstudy tree run under a system Electron on Linux.

The script is deliberately discovery-based.  Between releases the app has
moved packages between `node_modules`, `app.asar` and `app.asar.unpacked`, and
has changed the shape of its device-fingerprint code.  Nothing here assumes a
particular nesting depth, a particular package version, or a particular
source-code formatting.

What it does:

 1. cx-code-encryption -> Linux passthrough
    Replaces the package entry point(s) with a hook that resolves `.jscx`
    requires to the `.js` files produced by unpack.py and reports an empty
    SQLCipher key.  All copies of the package are found by name, and a stub is
    created if the package is absent.

 2. @journeyapps/sqlcipher Linux binding
    Reads the installed package's own `binary` metadata, derives the matching
    node-pre-gyp tarball URL, verifies the ELF, and installs it.  The version
    is no longer hard-coded, so a sqlcipher bump does not break the build.

 3. agora-electron-sdk / rkcloud-conference-electron-sdk / rk-native-record-node
    Native Windows/macOS-only RTC packages.  Their entry points are replaced
    with graceful "unsupported" stubs while all other files (notably
    `common_enum.js` and `NativeRecordInfo.js`) are left intact.

 4. wmic safety net
    A tiny preload is injected at the top of the application's main entry.  It
    makes `wmic` probes fail softly on non-Windows platforms.  This replaces
    the previous regex source rewriting, which produced broken code (a `let`
    declared inside an `if` block was still referenced outside it) and broke
    whenever the compiler changed its output formatting.

Usage: patch.py <app-root>
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import sys
import tarfile
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Package discovery
# --------------------------------------------------------------------------
APP_ROOT_PACKAGES = (
    "cx-code-encryption",
    "agora-electron-sdk",
    "rkcloud-conference-electron-sdk",
    "rk-native-record-node",
    "@journeyapps/sqlcipher",
)


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def safe_join(root: str, *parts: str) -> str:
    root_abs = os.path.abspath(root)
    dest = os.path.abspath(os.path.join(root_abs, *parts))
    if os.path.commonpath((root_abs, dest)) != root_abs:
        raise ValueError(f"path escapes {root}: {os.path.join(*parts)!r}")
    return dest


def find_packages(app_root: str, names: tuple[str, ...]) -> dict[str, list[tuple[str, dict]]]:
    """Return every directory whose package.json declares one of `names`."""
    wanted = set(names)
    found: dict[str, list[tuple[str, dict]]] = {name: [] for name in names}
    for dirpath, _dirnames, filenames in os.walk(app_root):
        if "package.json" not in filenames:
            continue
        try:
            pkg = read_json(os.path.join(dirpath, "package.json"))
        except (OSError, ValueError):
            continue
        name = pkg.get("name")
        if name in wanted:
            found[name].append((dirpath, pkg))
    return found


def resolve_entry(pkg_dir: str, pkg: dict) -> tuple[str, bool]:
    """Resolve the module file Node would load for `require(pkg.name)`.

    Returns (absolute entry path, whether package.json needs to be rewritten).
    Handles `main` as well as the simple forms of `exports`.
    """
    main = pkg.get("main") or "index.js"
    exports = pkg.get("exports")
    if isinstance(exports, str):
        main = exports
    elif isinstance(exports, dict):
        dot = exports.get(".")
        if isinstance(dot, str):
            main = dot
        elif isinstance(dot, dict):
            for key in ("require", "node", "default"):
                if isinstance(dot.get(key), str):
                    main = dot[key]
                    break

    while main.startswith("./"):
        main = main[2:]
    main = main or "index.js"

    candidates = [main]
    if not os.path.splitext(main)[1]:
        candidates += [main + ".js", main + ".cjs", main + "/index.js"]
    for candidate in candidates:
        try:
            target = safe_join(pkg_dir, candidate)
        except ValueError:
            continue
        if os.path.isfile(target):
            return target, False

    # The declared entry is missing: create index.js and point the package at it.
    return safe_join(pkg_dir, "index.js"), True


def point_package_at_index(pkg: dict) -> None:
    pkg["main"] = "index.js"
    exports = pkg.get("exports")
    if isinstance(exports, str):
        pkg["exports"] = "./index.js"
    elif isinstance(exports, dict):
        exports["."] = "./index.js"


def install_stub(app_root: str, name: str, source: str) -> list[str]:
    """Replace (or create) the entry point of package `name` with `source`."""
    found = find_packages(app_root, (name,))[name]
    targets: list[str] = []

    for pkg_dir, pkg in found:
        target, needs_update = resolve_entry(pkg_dir, pkg)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(source)
        if needs_update:
            point_package_at_index(pkg)
            write_json(os.path.join(pkg_dir, "package.json"), pkg)
        targets.append(target)

    if not targets:
        pkg_dir = safe_join(app_root, "node_modules", *name.split("/"))
        os.makedirs(pkg_dir, exist_ok=True)
        stub_pkg = {
            "name": name,
            "version": "0.0.0-linux-stub",
            "private": True,
            "main": "index.js",
            "exports": {
                ".": "./index.js",
                "./*": "./index.js",
                "./package.json": "./package.json",
            },
        }
        write_json(os.path.join(pkg_dir, "package.json"), stub_pkg)
        target = os.path.join(pkg_dir, "index.js")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(source)
        targets.append(target)

    return targets


# --------------------------------------------------------------------------
# cx-code-encryption replacement
# --------------------------------------------------------------------------
JSCX_HOOK = r'''"use strict";
/**
 * Linux replacement for cx-code-encryption.
 *
 * The vendor package loads a per-platform native addon.  This build already
 * decrypted every .jscx into a .js sibling, so only two behaviours are needed:
 * resolve .jscx imports to those .js files, and return the empty SQLCipher key
 * this package has always used on Linux.
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
    } catch (_) { /* ignore */ }
    return null;
}

if (!Module.__cxJscxHook) {
    const originalResolveFilename = Module._resolveFilename;
    Module._resolveFilename = function (request, parent, isMain, options) {
        try {
            return originalResolveFilename.call(this, request, parent, isMain, options);
        } catch (err) {
            if (!err || err.code !== "MODULE_NOT_FOUND") throw err;
            if (typeof request === "string" && request.endsWith(COMPILED_EXTNAME)) {
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
        if (!swapped) {
            throw new Error("[cx-linux] no decrypted module for " + filename);
        }
        fileModule._compile(fs.readFileSync(swapped, "utf8"), swapped);
    };

    Object.defineProperty(Module, "__cxJscxHook", { value: true });
}

/** Empty key = unencrypted database, matching previous Linux builds. */
function getDbKey(_dbname) {
    return "";
}
'''


# --------------------------------------------------------------------------
# RTC stubs
# --------------------------------------------------------------------------
AGORA_STUB = r'''"use strict";
/**
 * Linux stub for agora-electron-sdk (Windows/macOS native SDK).
 *
 * The live-classroom path only needs the module to load and to report that
 * RTC is unavailable; callers then take their own "not supported" branch.
 */
Object.defineProperty(exports, "__esModule", { value: true });

function noopProxy(name) {
    const fn = function () { return fn; };
    return new Proxy(fn, {
        get(_target, prop) {
            if (prop === "then") return undefined;
            if (prop === Symbol.toPrimitive) return () => name;
            if (prop === "toString") return () => `[stub:${name}]`;
            return noopProxy(`${name}.${String(prop)}`);
        },
        apply() { return noopProxy(name); },
        construct() { return {}; },
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
    console.warn("[cx-linux] agora RTC is not available on this platform");
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
 * Linux stub for rkcloud-conference-electron-sdk (Windows-only native SDK).
 *
 * Only the live-classroom path uses this.  The package's own `common_enum.js`
 * is left in place, so enum values imported from that subpath stay real.
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

exports.logInfo = (...args) => console.log("[rk-stub]", ...args);
exports.logWarn = (...args) => console.warn("[rk-stub]", ...args);
exports.logError = (...args) => console.error("[rk-stub]", ...args);
exports.enableDevTools = () => {};
exports.messageResult = () => ({ code: -1, message: "rkcloud stub: unsupported on linux" });
exports.getSDKVersion = () => "0.0.0-linux-stub";

class Holder { constructor(options = {}) { Object.assign(this, options); } }
exports.VideoOption = Holder;
exports.MsgData = Holder;
exports.RoomInfo = Holder;
exports.RtcConnection = Holder;
exports.Stats = Holder;
exports.User = Holder;
exports.DevInfoList = class DevInfoList { constructor() { this.mDevList = []; } };

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

RK_NATIVE_STUB = r'''"use strict";
/**
 * Linux stub for rk-native-record-node.
 *
 * The real package selects a Windows/macOS native plugin, or a
 * `build/RkNativeRecordPlugin.node` that this installer does not ship for
 * Linux.  Screen recording fails gracefully instead of crashing the process.
 * `NativeRecordInfo` (enums and data holders) is a separate file in the real
 * package and is left untouched when present.
 */
Object.defineProperty(exports, "__esModule", { value: true });

function noop() { return -1; }

class StubRecordEngine {
    constructor() { this.__isStub = true; }
    initEngine() { return -1; }
    destroyEngine() {}
    startRecord() { return -1; }
    stopRecord() { return -1; }
    pauseRecord() { return -1; }
    resumeRecord() { return -1; }
    setVideoOption() { return -1; }
    setAudioOption() { return -1; }
    getRecordInfo() { return null; }
}

function initRkNativePlugin() {
    console.warn("[cx-linux] rk native screen recording is not available on this platform");
}
function CreateRecordEngine() { return new StubRecordEngine(); }
function DestroyRecordEngine() {}

exports.initRkNativePlugin = initRkNativePlugin;
exports.CreateRecordEngine = CreateRecordEngine;
exports.DestroyRecordEngine = DestroyRecordEngine;

exports.MediaType = { MEDIA_NONE: 0, MEDIA_HLS: 1, MEDIA_MP4: 2, MEDIA_FLV: 3, MEDIA_RTMP: 4 };
exports.FPS = { FPS_10: 10, FPS_15: 15, FPS_24: 24, FPS_30: 30, FPS_60: 60 };
exports.Resolution = { RES_360P: 360, RES_480P: 480, RES_720P: 720, RES_1080P: 1080, RES_1440P: 1440, RES_4K: 2160 };
exports.ReasonType = { REASON_NONE: 0, REASON_POWER_EVENT: 3301 };
exports.AudioDeviceType = { PLAYOUT: 0, RECORDING: 1 };
exports.MediaDeviceState = { PLUGGED: 1, UNPLUGGED: 0 };
exports.RecordError = { SUCCESS: 0, FAILED: -1 };
exports.LiveError = { SUCCESS: 0, FAILED: -1 };

function Holder(options = {}) { Object.assign(this, options); }
exports.NativeRecordParms = Holder;
exports.AudioDevice = Holder;
exports.DesktopDevice = Holder;
exports.WindowRect = Holder;
exports.ResolutionAdaptiveParams = Holder;
exports.TestLogParams = Holder;

module.exports = new Proxy(module.exports, {
    get(target, prop) {
        if (prop in target) return target[prop];
        if (typeof prop === "symbol") return undefined;
        return noop;
    },
});
'''


# --------------------------------------------------------------------------
# Runtime shim: make wmic harmless on non-Windows platforms
# --------------------------------------------------------------------------
LINUX_SHIM = r'''"use strict";
/* cx-linux-shim: loaded before the application's main entry.
 *
 * Some releases assume "not darwin" means Windows and shell out to wmic.
 * On Linux that binary does not exist; depending on the caller it either
 * throws or returns garbage.  Report an empty result instead, which is what
 * the existing `if (!value)` fallbacks expect.
 */
const cp = require("child_process");
const { EventEmitter } = require("events");
const { PassThrough } = require("stream");

process.env.CX_LINUX_SHIM = __filename;

const MARK = Symbol.for("cx.linux.wmicShim");
const WMIC_RE = /(^|[\\/\s"'])wmic(\.exe)?(["'\s]|$)/i;
const warned = new Set();

function isWmic(args) {
    if (process.platform === "win32") return false;
    const command = args[0];
    if (typeof command !== "string") return false;
    if (!WMIC_RE.test(command)) return false;
    if (warned.size < 3 && !warned.has(command)) {
        warned.add(command);
        console.warn("[cx-linux] ignoring Windows-only command: " + command);
    }
    return true;
}

function emptyResult(options) {
    const encoding = typeof options === "string"
        ? options
        : (options && typeof options === "object" ? options.encoding : null);
    if (!encoding || encoding === "buffer") return Buffer.alloc(0);
    return "";
}

function fakeChild() {
    const child = new EventEmitter();
    child.pid = -1;
    child.killed = false;
    child.stdin = new PassThrough();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.kill = () => {
        child.killed = true;
        return true;
    };
    process.nextTick(() => {
        child.stdout.end();
        child.stderr.end();
        child.emit("exit", 1, null);
        child.emit("close", 1, null);
    });
    return child;
}

if (!cp[MARK]) {
    const originals = {
        execSync: cp.execSync,
        exec: cp.exec,
        execFile: cp.execFile,
        execFileSync: cp.execFileSync,
        spawn: cp.spawn,
        spawnSync: cp.spawnSync,
    };

    cp.execSync = function (command, options) {
        if (isWmic(arguments)) return emptyResult(options);
        return originals.execSync.apply(this, arguments);
    };
    cp.exec = function (command, options, callback) {
        if (isWmic(arguments)) {
            if (typeof options === "function") callback = options;
            const child = fakeChild();
            if (typeof callback === "function") {
                process.nextTick(() => callback(null, "", ""));
            }
            return child;
        }
        return originals.exec.apply(this, arguments);
    };
    cp.execFile = function (file, args, options, callback) {
        if (isWmic(arguments)) {
            if (typeof args === "function") callback = args;
            else if (typeof options === "function") callback = options;
            const child = fakeChild();
            if (typeof callback === "function") {
                process.nextTick(() => callback(null, "", ""));
            }
            return child;
        }
        return originals.execFile.apply(this, arguments);
    };
    cp.execFileSync = function (file, args, options) {
        if (isWmic(arguments)) return emptyResult(options);
        return originals.execFileSync.apply(this, arguments);
    };
    cp.spawn = function (file, args, options) {
        if (isWmic(arguments)) return fakeChild();
        return originals.spawn.apply(this, arguments);
    };
    cp.spawnSync = function (file, args, options) {
        if (isWmic(arguments)) {
            const stdout = emptyResult(options);
            const stderr = emptyResult(options);
            const error = Object.assign(
                new Error("wmic is not available on this platform"),
                { code: "ENOENT" }
            );
            return {
                pid: -1,
                output: [null, stdout, stderr],
                stdout,
                stderr,
                status: 1,
                signal: null,
                error,
            };
        }
        return originals.spawnSync.apply(this, arguments);
    };

    Object.defineProperty(cp, MARK, { value: true });
}

module.exports = { installed: true };
'''

SHIM_FILENAME = "cx-linux-shim.cjs"
SHIM_MARKER = "cx-linux-shim"
SHIM_PRELOAD = (
    "if (process.env.CX_LINUX_SHIM) {\n"
    "    try { require(process.env.CX_LINUX_SHIM); } catch (_) { /* ignore */ }\n"
    "}\n"
)


def inject_js_statement(source: str, statement: str) -> str:
    """Insert `statement` at the top of a script without breaking directives."""
    if source.startswith("#!"):
        newline = source.find("\n")
        if newline == -1:
            return source + "\n" + statement
        return source[: newline + 1] + statement + source[newline + 1 :]

    match = re.match(r'^(\s*)(["\'])use strict\2\s*;?[ \t]*\r?\n', source)
    if match:
        return source[: match.end()] + statement + source[match.end() :]
    return statement + source


def install_shim(app_root: str) -> tuple[bool, str | None]:
    """Write the shim and load it before the application's main entry."""
    try:
        pkg = read_json(os.path.join(app_root, "package.json"))
    except (OSError, ValueError):
        pkg = {}

    main = pkg.get("main") or "index.js"
    while main.startswith("./"):
        main = main[2:]
    entry = safe_join(app_root, main)
    if os.path.isdir(entry):
        entry = os.path.join(entry, "index.js")
    if not os.path.isfile(entry):
        fallback = safe_join(app_root, "index.js")
        if os.path.isfile(fallback):
            entry = fallback
        else:
            print(f"ERROR: cannot find the main entry ({main})", file=sys.stderr)
            return False, None

    shim_path = safe_join(app_root, SHIM_FILENAME)
    with open(shim_path, "w", encoding="utf-8") as fh:
        fh.write(LINUX_SHIM)

    with open(entry, encoding="utf-8") as fh:
        source = fh.read()
    if SHIM_MARKER in source[:4096]:
        return True, entry

    is_esm = pkg.get("type") == "module" or entry.endswith((".mjs", ".mts"))
    statement = (
        f'import "./{SHIM_FILENAME}";\n'
        if is_esm
        else f'require("./{SHIM_FILENAME}");\n'
    )
    with open(entry, "w", encoding="utf-8") as fh:
        fh.write(inject_js_statement(source, statement))
    return True, entry


def install_shim_preloads(app_root: str) -> int:
    """Load the shim in renderer/utility processes that use MachineIdUtil.

    The main entry only covers the main process.  `@cx/cxcore`'s machine-id
    helper is also required from preloads and utility processes, which do not
    inherit the main module cache.  Inject one guarded require there; the shim
    exports its own path through CX_LINUX_SHIM for those processes to find.
    """
    count = 0
    for dirpath, _dirnames, filenames in os.walk(app_root):
        if "MachineIdUtil.js" not in filenames:
            continue
        path = os.path.join(dirpath, "MachineIdUtil.js")
        try:
            with open(path, encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            continue
        if "CX_LINUX_SHIM" in source[:4096]:
            count += 1
            continue
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(inject_js_statement(source, SHIM_PRELOAD))
        count += 1
    return count


# --------------------------------------------------------------------------
# SQLCipher prebuilt binding
# --------------------------------------------------------------------------
ELF_MACHINE_BY_ARCH = {
    "x64": 62,    # EM_X86_64
    "ia32": 3,    # EM_386
    "arm64": 183, # EM_AARCH64
    "arm": 40,    # EM_ARM
}
NODE_ARCH_BY_MACHINE = {
    "x86_64": "x64",
    "amd64": "x64",
    "i686": "ia32",
    "i386": "ia32",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
    "armv8l": "arm64",
}


def node_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "win32"
    return sys.platform


def node_arch() -> str:
    machine = platform.machine().lower()
    if machine in NODE_ARCH_BY_MACHINE:
        return NODE_ARCH_BY_MACHINE[machine]
    raise RuntimeError(f"unsupported machine architecture: {machine}")


def elf_machine(path: str) -> int | None:
    try:
        with open(path, "rb") as fh:
            head = fh.read(20)
    except OSError:
        return None
    if len(head) < 20 or head[:4] != b"\x7fELF" or head[4] != 2:
        return None
    return struct.unpack("<H", head[18:20])[0]


def usable_binding(path: str, arch: str) -> bool:
    return elf_machine(path) == ELF_MACHINE_BY_ARCH.get(arch)


def sqlcipher_napi_version(pkg_dir: str, pkg: dict) -> int:
    versions = []
    for path in glob.glob(os.path.join(pkg_dir, "lib", "binding", "napi-v*")):
        match = re.search(r"napi-v(\d+)-", os.path.basename(path))
        if match:
            versions.append(int(match.group(1)))
    if versions:
        return max(versions)

    binary = pkg.get("binary") or {}
    declared = binary.get("napi_versions")
    if isinstance(declared, list) and declared:
        return max(int(value) for value in declared)
    return 6


def sqlcipher_modules_version(pkg_dir: str) -> str:
    versions = []
    for path in glob.glob(os.path.join(pkg_dir, "lib", "binding", "node-v*")):
        match = re.search(r"node-v(\d+)-", os.path.basename(path))
        if match:
            versions.append(int(match.group(1)))
    return str(max(versions)) if versions else ""


def cache_directory() -> str:
    srcdest = os.environ.get("SRCDEST")
    if srcdest:
        return srcdest
    return os.path.join(os.path.expanduser("~"), ".cache", "chaoxing-portable")


def download(url: str, dest: str, retries: int = 3) -> None:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "chaoxing-portable/1"}
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                with open(dest + ".part", "wb") as fh:
                    shutil.copyfileobj(response, fh, 1 << 20)
            os.replace(dest + ".part", dest)
            return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            print(f"  download attempt {attempt}/{retries} failed: {exc}", file=sys.stderr)
            if attempt < retries:
                time.sleep(min(2 * attempt, 5))
    raise RuntimeError(f"download failed: {last_error}")


def sqlcipher_urls(pkg: dict, napi: int, modules: str, platform_name: str, arch: str) -> list[str]:
    binary = pkg.get("binary") or {}
    host = (binary.get("host") or "https://journeyapps-node-binary.s3.amazonaws.com").rstrip("/")
    name = pkg.get("name") or "@journeyapps/sqlcipher"
    version = pkg.get("version") or "0.0.0"
    template = binary.get("package_name") or "napi-v{napi_build_version}-{platform}-{arch}.tar.gz"

    values = {
        "name": name,
        "version": version,
        "napi_build_version": napi,
        "modules": modules,
        "platform": platform_name,
        "arch": arch,
        "toolset": f"napi-v{napi}" if "napi" in template else f"node-v{modules}",
        "module_name": binary.get("module_name") or "node_sqlite3",
    }
    try:
        package_name = template.format(**values)
    except (KeyError, IndexError):
        package_name = f"napi-v{napi}-{platform_name}-{arch}.tar.gz"

    urls = [f"{host}/{name}/v{version}/{package_name}"]

    remote_path = binary.get("remote_path")
    if isinstance(remote_path, str) and remote_path.strip():
        try:
            rel = remote_path.format(**values).strip("/")
            candidate = f"{host}/{rel}/{package_name}"
            if candidate not in urls:
                urls.append(candidate)
        except (KeyError, IndexError):
            pass
    return urls


def fetch_sqlcipher_binding(
    pkg_dir: str, pkg: dict, platform_name: str, arch: str
) -> str | None:
    existing = glob.glob(os.path.join(pkg_dir, "lib", "binding", "*-linux-*", "*.node"))
    existing += glob.glob(os.path.join(pkg_dir, "build", "Release", "*.node"))
    for path in existing:
        if usable_binding(path, arch):
            return path

    napi = sqlcipher_napi_version(pkg_dir, pkg)
    modules = sqlcipher_modules_version(pkg_dir)
    urls = sqlcipher_urls(pkg, napi, modules, platform_name, arch)

    cache = cache_directory()
    os.makedirs(cache, exist_ok=True)
    version = pkg.get("version") or "0"
    tarball = os.path.join(
        cache, f"chaoxing-sqlcipher-{version}-napi-v{napi}-{platform_name}-{arch}.tar.gz"
    )

    if not os.path.isfile(tarball):
        last_error: Exception | None = None
        for url in urls:
            try:
                print(f"  fetching {url}")
                download(url, tarball)
                last_error = None
                break
            except RuntimeError as exc:
                last_error = exc
        if last_error is not None:
            print(f"ERROR: {last_error}", file=sys.stderr)
            return None

    expected_hash = os.environ.get("CHAOXING_SQLCIPHER_SHA256", "").strip()
    if expected_hash:
        with open(tarball, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        if digest != expected_hash:
            try:
                os.remove(tarball)
            except OSError:
                pass
            print(
                f"ERROR: sqlcipher tarball checksum mismatch\n"
                f"  expected {expected_hash}\n  got      {digest}\n"
                f"  removed the cached tarball; retry to download it again",
                file=sys.stderr,
            )
            return None

    binary = pkg.get("binary") or {}
    module_name = (binary.get("module_name") or "node_sqlite3") + ".node"
    blob = None
    try:
        with tarfile.open(tarball, "r:gz") as archive:
            members = [
                member
                for member in archive.getmembers()
                if member.isfile() and member.name.endswith(module_name)
            ]
            if not members:
                members = [
                    member
                    for member in archive.getmembers()
                    if member.isfile() and member.name.endswith(".node")
                ]
            if not members:
                print("ERROR: no native binding inside the sqlcipher tarball", file=sys.stderr)
                return None
            member = members[0]
            if member.size > 64 * 1024 * 1024:
                print("ERROR: sqlcipher binding is unexpectedly large", file=sys.stderr)
                return None
            extracted = archive.extractfile(member)
            if extracted is None:
                print("ERROR: cannot read the sqlcipher binding", file=sys.stderr)
                return None
            blob = extracted.read()
    except (tarfile.TarError, OSError) as exc:
        print(f"ERROR: cannot unpack {tarball}: {exc}", file=sys.stderr)
        return None

    expected_machine = ELF_MACHINE_BY_ARCH.get(arch)
    if (
        len(blob) < 20
        or blob[:4] != b"\x7fELF"
        or blob[4] != 2
        or struct.unpack("<H", blob[18:20])[0] != expected_machine
    ):
        print("ERROR: fetched sqlcipher binding is not a matching Linux ELF", file=sys.stderr)
        return None

    module_path = (binary.get("module_path") or "./lib/binding/napi-v{napi_build_version}-{platform}-{arch}").format(
        napi_build_version=napi,
        modules=modules,
        platform=platform_name,
        arch=arch,
        name=pkg.get("name") or "",
        version=version,
        toolset="",
        module_name=binary.get("module_name") or "node_sqlite3",
    )
    module_path = module_path.strip("/")
    dest_dir = safe_join(pkg_dir, module_path)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, module_name)
    with open(dest + ".part", "wb") as fh:
        fh.write(blob)
    os.replace(dest + ".part", dest)
    os.chmod(dest, 0o755)
    return dest


def install_sqlcipher(app_root: str) -> tuple[int, int]:
    found = find_packages(app_root, ("@journeyapps/sqlcipher",))["@journeyapps/sqlcipher"]
    if not found:
        print("  warning: @journeyapps/sqlcipher is not present", file=sys.stderr)
        return 0, 0

    try:
        platform_name = node_platform()
        arch = node_arch()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 0, len(found)

    installed = 0
    for pkg_dir, pkg in found:
        print(f"  sqlcipher {pkg.get('version')} at {os.path.relpath(pkg_dir, app_root)}")
        try:
            result = fetch_sqlcipher_binding(pkg_dir, pkg, platform_name, arch)
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            result = None
        if result:
            print(f"    binding: {os.path.relpath(result, app_root)}")
            installed += 1
        else:
            print(f"ERROR: no usable Linux binding for {pkg_dir}", file=sys.stderr)
    return installed, len(found)


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app_root", help="path to the unpacked application tree")
    args = parser.parse_args()

    app_root = os.path.abspath(args.app_root)
    if not os.path.isdir(app_root):
        print(f"ERROR: {app_root} is not a directory", file=sys.stderr)
        return 2

    print(f"patching {app_root}")

    encryption = install_stub(app_root, "cx-code-encryption", JSCX_HOOK)
    print(f"  cx-code-encryption entry replaced ({len(encryption)} copy/copies)")

    rtc_targets = []
    for name, source in (
        ("agora-electron-sdk", AGORA_STUB),
        ("rkcloud-conference-electron-sdk", RK_STUB),
        ("rk-native-record-node", RK_NATIVE_STUB),
    ):
        targets = install_stub(app_root, name, source)
        rtc_targets.extend(targets)
        print(f"  {name} entry replaced ({len(targets)} copy/copies)")

    sqlcipher_installed, sqlcipher_found = install_sqlcipher(app_root)

    shim_ok, entry = install_shim(app_root)
    if shim_ok:
        print(f"  wmic shim injected into {os.path.relpath(entry, app_root)}")

    shim_preloads = install_shim_preloads(app_root)
    print(f"  wmic shim preloaded in {shim_preloads} machine-id module(s)")

    failed = False
    if not encryption:
        print("ERROR: cx-code-encryption could not be installed", file=sys.stderr)
        failed = True
    if len(rtc_targets) < 3:
        print("ERROR: one or more RTC stubs could not be installed", file=sys.stderr)
        failed = True
    if sqlcipher_found and sqlcipher_installed != sqlcipher_found:
        print("ERROR: sqlcipher binding is missing", file=sys.stderr)
        failed = True
    if not shim_ok:
        failed = True

    print(
        f"summary: encryption={len(encryption)} rtc={len(rtc_targets)} "
        f"sqlcipher={sqlcipher_installed}/{sqlcipher_found} shim={shim_ok}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
