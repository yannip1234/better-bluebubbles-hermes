#!/usr/bin/env python3
"""
Idempotent patcher for BlueBubbles.app on macOS Sequoia / Tahoe (15+) to enable
arbitrary emoji tapbacks (IMAssociatedMessageTypeAcknowledgmentEmoji = 2006 / 3006)
without forking or rebuilding BlueBubbles from source.

What this script patches:
1. /Applications/BlueBubbles.app/Contents/Resources/app.asar:
   - Relaxes the REST `sendReactionRules` validator (`reaction: "required|string"`)
     and Socket.IO `send-reaction` tapback allowlist in `/dist/main.js` using
     exact-byte-length in-place replacements, and updates `/dist/main.js`'s
     SHA256 integrity hash in the asar header.
2. /Applications/BlueBubbles.app/Contents/Resources/appResources/private-api/macos11/BlueBubblesHelper.dylib:
   - Moves the original helper dylib to `BlueBubblesHelperOrig.dylib` (once) and
     compiles `native/BlueBubblesEmojiTapback.m` as a Mach-O wrapper dylib that
     re-exports `BlueBubblesHelperOrig.dylib` and swizzles:
       * +[BlueBubblesHelper parseReactionType:]
       * +[BlueBubblesHelper reactionToVerb:]
       * -[IMMessage initWithSender:time:text:messageSubject:fileTransferGUIDs:flags:error:guid:subject:associatedMessageGUID:associatedMessageType:associatedMessageRange:messageSummaryInfo:]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

REST_RULE_OLD = b'reaction:`required|string|in:${Mu.possibleReactions.join(",")}`'
REST_RULE_NEW = b'reaction:"required|string"'.ljust(len(REST_RULE_OLD), b" ")

SOCKET_RULE_OLD = (
    b'if(!e?.tapback||!["love","like","dislike","laugh","emphasize","question",'
    b'"-love","-like","-dislike","-laugh","-emphasize","-question"].includes(e.tapback))'
)
SOCKET_RULE_NEW = b'if(!e?.tapback||typeof e.tapback!=="string"'.ljust(len(SOCKET_RULE_OLD) - 1, b" ") + b")"


def _is_wrapper_dylib(dylib_path: Path) -> bool:
    if not dylib_path.exists():
        return False
    res = subprocess.run(
        ["otool", "-L", str(dylib_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return "BlueBubblesHelperOrig.dylib" in res.stdout


def patch_asar(asar_path: Path) -> bool:
    bak_path = asar_path.with_suffix(".asar.bak")
    if not bak_path.exists():
        shutil.copy2(asar_path, bak_path)
        print(f"[+] Backed up {asar_path} -> {bak_path}")

    raw = bytearray(asar_path.read_bytes())
    _, header_size, _, json_size = struct.unpack("<IIII", raw[:16])
    header_bytes = bytes(raw[16 : 16 + json_size])
    header = json.loads(header_bytes.decode("utf-8"))
    data_offset = 8 + header_size

    dist_node = header["files"]["dist"]["files"]["main.js"]
    main_rel_offset = int(dist_node["offset"])
    main_size = int(dist_node["size"])
    main_start = data_offset + main_rel_offset
    main_end = main_start + main_size

    main_bytes = bytes(raw[main_start:main_end])
    old_hash = dist_node.get("integrity", {}).get("hash", "")

    patched_main = main_bytes
    changed = False

    if REST_RULE_OLD in patched_main:
        assert len(REST_RULE_OLD) == len(REST_RULE_NEW)
        patched_main = patched_main.replace(REST_RULE_OLD, REST_RULE_NEW, 1)
        changed = True
        print("[+] Patched REST sendReactionRules in /dist/main.js")
    elif REST_RULE_NEW in patched_main:
        print("[=] REST sendReactionRules already patched in /dist/main.js")
    else:
        raise RuntimeError("Could not locate sendReactionRules pattern in /dist/main.js")

    if SOCKET_RULE_OLD in patched_main:
        assert len(SOCKET_RULE_OLD) == len(SOCKET_RULE_NEW)
        patched_main = patched_main.replace(SOCKET_RULE_OLD, SOCKET_RULE_NEW, 1)
        changed = True
        print("[+] Patched Socket.IO send-reaction allowlist in /dist/main.js")
    elif SOCKET_RULE_NEW in patched_main:
        print("[=] Socket.IO send-reaction allowlist already patched in /dist/main.js")

    if changed:
        assert len(patched_main) == len(main_bytes)
        raw[main_start:main_end] = patched_main
        new_hash = hashlib.sha256(patched_main).hexdigest()
        if old_hash and len(old_hash) == len(new_hash):
            patched_header = header_bytes.replace(old_hash.encode("ascii"), new_hash.encode("ascii"))
            assert len(patched_header) == len(header_bytes)
            raw[16 : 16 + json_size] = patched_header
            print(f"[+] Updated /dist/main.js SHA256 integrity hash: {old_hash[:12]}... -> {new_hash[:12]}...")

        tmp_path = asar_path.with_suffix(".asar.tmp")
        tmp_path.write_bytes(raw)
        os.replace(tmp_path, asar_path)
        print(f"[+] Wrote patched {asar_path}")
    return changed


def patch_helper_dylib(dylib_dir: Path, source_file: Path) -> None:
    dylib_path = dylib_dir / "BlueBubblesHelper.dylib"
    orig_path = dylib_dir / "BlueBubblesHelperOrig.dylib"

    if not dylib_path.exists() and not orig_path.exists():
        raise FileNotFoundError(f"Neither {dylib_path} nor {orig_path} exists")

    if not _is_wrapper_dylib(dylib_path):
        shutil.copy2(dylib_path, orig_path)
        print(f"[+] Backed up original helper dylib -> {orig_path}")
    else:
        print(f"[=] {orig_path} already holds original BlueBubblesHelper.dylib")

    out_tmp = dylib_dir / "BlueBubblesHelper.dylib.new"
    cmd = [
        "clang",
        "-dynamiclib",
        "-arch",
        "arm64e",
        "-arch",
        "arm64",
        "-arch",
        "x86_64",
        "-O2",
        "-fobjc-arc",
        "-framework",
        "Foundation",
        f"-Wl,-reexport_library,{orig_path}",
        "-install_name",
        str(dylib_path),
        str(source_file),
        "-o",
        str(out_tmp),
    ]
    print("[+] Compiling wrapper BlueBubblesHelper.dylib ...")
    subprocess.run(cmd, check=True)
    subprocess.run(["codesign", "-f", "-s", "-", str(out_tmp)], check=True)
    os.replace(out_tmp, dylib_path)
    print(f"[+] Installed and signed {dylib_path}")


def restore_all(app_path: Path) -> None:
    res_dir = app_path / "Contents" / "Resources"
    asar_path = res_dir / "app.asar"
    bak_path = asar_path.with_suffix(".asar.bak")
    if bak_path.exists():
        shutil.copy2(bak_path, asar_path)
        print(f"[+] Restored {asar_path} from {bak_path}")

    dylib_dir = res_dir / "appResources" / "private-api" / "macos11"
    dylib_path = dylib_dir / "BlueBubblesHelper.dylib"
    orig_path = dylib_dir / "BlueBubblesHelperOrig.dylib"
    if orig_path.exists():
        shutil.copy2(orig_path, dylib_path)
        print(f"[+] Restored {dylib_path} from {orig_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch BlueBubbles.app for arbitrary emoji tapbacks.")
    parser.add_argument(
        "--app-path",
        type=Path,
        default=Path("/Applications/BlueBubbles.app"),
        help="Path to BlueBubbles.app (default: /Applications/BlueBubbles.app)",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="Restore original app.asar and BlueBubblesHelper.dylib from backups",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    source_file = repo_root / "native" / "BlueBubblesEmojiTapback.m"

    if args.restore:
        restore_all(args.app_path)
        return 0

    if not source_file.exists():
        raise FileNotFoundError(f"Missing Objective-C source file: {source_file}")

    res_dir = args.app_path / "Contents" / "Resources"
    asar_path = res_dir / "app.asar"
    dylib_dir = res_dir / "appResources" / "private-api" / "macos11"

    patch_asar(asar_path)
    patch_helper_dylib(dylib_dir, source_file)
    print("[+] Done. Restart BlueBubbles.app and Messages.app to load the patched helper.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
