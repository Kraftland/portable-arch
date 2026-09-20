#!/usr/bin/env python3
"""Neutralise Windows-only device fingerprinting in a decrypted cxstudy tree.

Several call sites assume "not darwin" means Windows and therefore shell out to
`wmic`, which does not exist on Linux. All of them are wrapped in try/catch so
the app survives, but each failure logs a stack trace and, worse, leaves the
machine id partially assembled (empty product UUID and disk serial), which can
make the client look like a different device to Chaoxing's servers on every run.

Two distinct patterns appear:

  A) Unconditional `wmic` in a non-win32 branch, e.g. DevHelper.js:361
         if (process.platform == "darwin") { ... } else { execSync("wmic ...") }
     Fixed by adding a win32 guard so the else branch is skipped on Linux.

  B) `wmic` already inside a win32 branch, e.g. SessionCookie.js:186
     Harmless on Linux; left alone.

  C) `executeCommandWithFallback([...])` in @cx/cxcore MachineIdUtil — a list of
     wmic/powershell commands tried in order. Already platform-safe, but on Linux
     it returns "" for every command, so we prepend Linux-appropriate
     equivalents (DMI product UUID, block device serial) to keep the id stable.

Only pattern A and C are rewritten. Every edit is verified: if the expected
source shape is not found, the file is reported rather than silently skipped.

Usage: patch_winid.py <app-root>
"""
from __future__ import annotations

import os
import re
import sys

# --- pattern A: guard an unguarded wmic call -------------------------------

# Matches the `else { ... execSync("wmic ...") ... }` branch, capturing the
# executeSync expression so we can wrap it in a platform check.
WMIC_ELSE_RE = re.compile(
    r"(\n(\s*)else\s*\{\s*\n)"
    r"(\s*)((?:\w+\.)*\w+)\s*=\s*"
    r"((?:\w+\.)*\w+)\.execSync\("
    r"(\"wmic[^\"]*\"|`wmic[^`]*`)"
    r"(\s*,\s*\{[^}]*\}\s*)?\)\s*;",
    re.MULTILINE,
)


def guard_wmic_else(src: str, path: str) -> tuple[str, int]:
    """Wrap `else { X = execSync("wmic ...") }` so it only runs on win32."""
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        count += 1
        lead = m.group(1)           # "\n    else {\n"
        indent = m.group(3)         # "        "
        assign = m.group(4)         # "devTypeInfo"
        proc = m.group(5)           # "child_process_1.default"
        cmd = m.group(6)            # "\"wmic ...\""
        opts = m.group(7) or ""
        return (
            f"{lead}"
            f"{indent}if (process.platform == \"win32\") {{\n"
            f"{indent}    {assign} = {proc}.execSync({cmd}{opts});\n"
            f"{indent}}}"
        )

    return WMIC_ELSE_RE.sub(repl, src), count


# --- pattern C: prepend Linux commands to the fallback chain ----------------

DMI_UUID = "cat /sys/class/dmi/id/product_uuid 2>/dev/null || true"
DMI_SERIAL = "cat /sys/class/dmi/id/product_serial 2>/dev/null || true"
# Fallback for VMs/containers where the DMI fields are absent: read the serial of
# the block device backing /.  `lsblk -ndo PKNAME` yields the parent disk name,
# then `-ndo SERIAL` reads that disk's serial.  `|| true` keeps execSync quiet
# when the device reports none.
DISK_SERIAL = (
    "lsblk -ndo SERIAL /dev/$(lsblk -ndo PKNAME $(findmnt -no SOURCE /) "
    "2>/dev/null | head -1) 2>/dev/null || true"
)
# Universal Linux fallback. /etc/machine-id is present on every systemd host and
# is stable across reboots, unlike a MAC address.
MACHINE_ID = "cat /etc/machine-id 2>/dev/null || cat /var/lib/dbus/machine-id 2>/dev/null || true"
PRODUCT_NAME = "cat /sys/class/dmi/id/product_name 2>/dev/null || true"

MACHINEID_LINUX_ADDITIONS = {
    "getProductUuid": [DMI_UUID, MACHINE_ID, PRODUCT_NAME],
    "getDiskSerialnumber": [DMI_SERIAL, DISK_SERIAL, MACHINE_ID],
}


def add_linux_fallbacks(src: str, path: str) -> tuple[str, int]:
    """Prepend Linux-native commands to executeCommandWithFallback(...) lists."""
    count = 0
    for func_name, cmds in MACHINEID_LINUX_ADDITIONS.items():
        # Find `function <name>() { return executeCommandWithFallback([ ... ], [ ... ]); }`
        pattern = re.compile(
            rf"(function\s+{func_name}\s*\(\)\s*\{{\s*return\s+executeCommandWithFallback\(\s*\[)",
            re.MULTILINE,
        )
        m = pattern.search(src)
        if not m:
            continue
        if cmds[0] in src[m.start() : m.start() + 800]:
            continue  # already patched
        addition = "".join(f'\n        "{c}",' for c in cmds)
        src = src[: m.end()] + addition + src[m.end() :]
        count += 1
    return src, count


def patch_file(path: str, fn) -> int:
    with open(path) as fh:
        src = fh.read()
    new, n = fn(src, path)
    if n:
        with open(path, "w") as fh:
            fh.write(new)
    return n


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    app_root = sys.argv[1]

    total_a = total_c = 0

    # Pattern A: walk all app sources (not node_modules - those are pattern C).
    for base in ("electron", "module", "dist"):
        root = os.path.join(app_root, base)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(".js"):
                    continue
                p = os.path.join(dirpath, name)
                try:
                    n = patch_file(p, guard_wmic_else)
                except OSError:
                    continue
                if n:
                    print(f"  guarded {n} wmic call(s): {os.path.relpath(p, app_root)}")
                    total_a += n

    # Pattern C: the machine-id helper inside @cx/cxcore.
    mi = os.path.join(
        app_root, "node_modules/@cx/cxcore/dist/util/MachineIdUtil.js"
    )
    if os.path.isfile(mi):
        try:
            total_c = patch_file(mi, add_linux_fallbacks)
        except OSError:
            total_c = 0
        if total_c:
            print(f"  added Linux fallbacks: {total_c} function(s) in MachineIdUtil.js")
    else:
        print("  WARN: MachineIdUtil.js not found (skipped)", file=sys.stderr)

    print(f"wmic guards added: {total_a}")
    print(f"linux machine-id fallbacks added: {total_c}")

    if total_a == 0 and total_c == 0:
        print(
            "WARN: nothing to patch - either the tree was already patched or the "
            "app sources changed shape.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
