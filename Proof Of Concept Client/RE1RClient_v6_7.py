from __future__ import annotations

"""
Resident Evil 1 Remake Archipelago Client v6.7
============================================

Current scope:
- Attach to bhd.exe.
- Wait until an actual room is loaded.
- Read the persistent Group 3 flag bank (64 bytes).
- Resolve the minimal flag->location-name catalog against the Archipelago
  data package.
- Send LocationChecks for whitelisted flags that are set.
- For AP-randomized physical pickups, bypass vanilla inventory-capacity
  rejection and skip the vanilla inventory reward while preserving the game's
  own CompleteItemAcquisition path.
- Receive real RE1R AP items and translate their stable AP item IDs through
  seed slot_data into native RE1R item ID + quantity deliveries.
- Deliver every AP item directly into the proven 256-slot shared Item Box;
  never place AP deliveries into Jill's carried inventory. Existing Item Box
  stacks are filled first, and an AP item remains pending if the box cannot
  fit the complete delivery.
- Bind a guarded virtual Item Box hotkey (default F8) that invokes the exact
  runtime-proven vanilla box-opening sequence without requiring a physical box.
- Survive room transitions, title screens, process restarts, and AP reconnects.
- Detect true game completion by polling the runtime-confirmed active aEnding engine state and report CLIENT_GOAL.
- Detect runtime-confirmed RE1R save-slot loads and restore post-save AP ReceivedItems into the native Item Box without rewinding the normal AP delivery cursor.
- On a real save-slot load, immediately reapply every AP-checked randomized Group-3 location flag before the load-state apply function returns, so world pickups collected after the older RE1R save stay gone.
- Detect a Fresh New Game from the runtime-confirmed aGame constructor ordering. If no selected-slot load preceded that constructor, treat the new game as an implicit AP cursor-0 baseline, restore already-checked randomized Group-3 flags during initialization, and replay all previously delivered AP ReceivedItems into the native Item Box.

The AP pickup hooks are armed together with real AP item delivery. Until
/re1rarm (or --replay-received-items) is used, those gameplay-changing hooks
remain disabled. The save tracker installs separate, behavior-preserving CALL
instrumentation on attach so confirmed typewriter saves can be recorded.

Still intentionally deferred:
- Direct in-place death retry paths that neither load a save slot nor construct a new aGame, if any.
- DeathLink.
- Other Jill difficulties and Chris support.

Run this file from an Archipelago installation/source tree so CommonClient.py,
Utils.py, NetUtils.py, etc. are importable.

Requires pymem:
    pip install pymem

Example:
    python RE1RClient_v6_7.py --connect localhost:38281 --name Player1

The default location catalog is expected next to this script:
    RE1R_Jill_Normal_locations_minimal.json
"""

import asyncio
import ctypes
import json
import hashlib
import logging
import struct
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import ModuleUpdate
ModuleUpdate.update()

import Utils

if __name__ == "__main__":
    Utils.init_logging("RE1RClient", exception_logger="Client")

import pymem
import pymem.process

from NetUtils import ClientStatus

from CommonClient import (
    ClientCommandProcessor,
    CommonContext,
    get_base_parser,
    gui_enabled,
    logger,
    server_loop,
)


# ---------------------------------------------------------------------------
# Game / client configuration
# ---------------------------------------------------------------------------

GAME_NAME = "Resident Evil 1 Remake"
PROCESS_NAME = "bhd.exe"

DEFAULT_LOCATION_FILE = Path(__file__).with_name(
    "RE1R_Jill_Normal_locations_minimal_v2.json"
)

DEFAULT_POLL_INTERVAL = 0.10
DEFAULT_ITEMBOX_HOTKEY = "F8"
DEFAULT_SAVE_TRACKING_FILE = Path.home() / "RE1R_Archipelago_save_tracking.json"
FUNCTION_KEY_VKS = {f"F{i}": 0x70 + (i - 1) for i in range(1, 13)}


# ---------------------------------------------------------------------------
# Confirmed RE1R memory layout
# ---------------------------------------------------------------------------

STATE_ROOT_PTR_RVA = 0x97CA2C

# Embedded runtime RoomRecord array in StateRoot.
ROOM_RECORD_COUNT_OFFSET = 0x474
ROOM_RECORD_CAPACITY_OFFSET = 0x478
ROOM_RECORD_DATA_OFFSET = 0x480

# Persistent flag group 3.
GROUP3_OFFSET = 0x2DC
GROUP3_SIZE = 0x40
GROUP3_FLAG_COUNT = GROUP3_SIZE * 8

# Runtime-confirmed engine-state victory detector.
#
# sBhdMain is registered as a 0x28338-byte engine root object. Its live
# singleton pointer is stored at bhd.exe+0x9E4248. The constructor stores an
# sArea-derived manager at +0x282A0. That manager owns an active-state array:
#   +0x3824 active state count
#   +0x382C active state pointers[8]
#
# Successful Jill endings with and without the final Tyrant both produce an
# active aEnding instance (vtable bhd.exe+0x875190). A final-Tyrant death
# transitions to aInit instead. The old Signal Rockets / Heliport unload
# heuristic is therefore retired.
SBHDMAIN_PTR_RVA = 0x9E4248
SBHDMAIN_AREA_MANAGER_OFFSET = 0x282A0
SBHDMAIN_VTABLE_RVA = 0x8811C8

AREA_MANAGER_VTABLE_RVA = 0x880860
AREA_ACTIVE_COUNT_OFFSET = 0x3824
AREA_ACTIVE_STATES_OFFSET = 0x382C
AREA_MAX_ACTIVE_STATES = 8

AENDING_VTABLE_RVA = 0x875190


# ---------------------------------------------------------------------------
# Confirmed native item-delivery layout
# ---------------------------------------------------------------------------

CONTEXT_PTR_RVA = 0x97C9C0

# Runtime-confirmed primary room identity fields.
CONTEXT_STAGE_OFFSET = 0xE472C
CONTEXT_ROOM_OFFSET = 0xE4730

# Virtual Item Box access is intentionally restricted to the canonical
# Mansion room code 0x112: stage 1, room 0x12, West Wing Outer Stairway.
VIRTUAL_ITEMBOX_STAGE = 0x1
VIRTUAL_ITEMBOX_ROOM = 0x12
VIRTUAL_ITEMBOX_ROOM_NAME = "Mansion - West Wing Outer Stairway"

# Native setters proven by live injection tests.
SET_INVENTORY_SLOT_RVA = 0x8C9F0   # VA 0x0048C9F0
SET_ITEMBOX_SLOT_RVA = 0x30120      # VA 0x00430120

# Runtime-proven virtual Item Box open path.
# Physical box interaction executes:
#   FUN_0048B740(Context, 7)                 -> clear Context+0x30 bit 7
#   FUN_004939E0(UIManager, 5,0,0,0,0,1)    -> open native Item Box UI
UI_MANAGER_PTR_RVA = 0x9815A4
CLEAR_CONTEXT_FLAG_RVA = 0x8B740
OPEN_UI_RVA = 0x939E0
ITEMBOX_OPEN_CLEAR_FLAG = 7

# Active aGame mode guard. Runtime traces establish +0x31C as the current
# main-game mode; mode 4 is ordinary gameplay, while modal UI uses mode 7.
AGAME_VTABLE_RVA = 0x875340
AGAME_CURRENT_MODE_OFFSET = 0x31C
AGAME_NORMAL_GAMEPLAY_MODE = 4

# Item metadata table used by the game's own inventory logic.
ITEM_TABLE_RVA = 0x97CAB0
ITEM_RECORD_SIZE = 0x90
ITEM_FLAGS_OFFSET = 0x50

# Strongly supported by the native pickup path and live stack tests.
ITEM_FLAG_STACKABLE = 0x04

INVENTORY_SLOTS_OFFSET = 0x38
JILL_INVENTORY_SLOTS = 8

ITEMBOX_BASE_OFFSET = 0x88
ITEMBOX_BANK = 0
ITEMBOX_SLOTS = 0x100
ITEMBOX_BANK_STRIDE = 0x800

SLOT_SIZE = 8
MAX_GAME_ITEM_ID = 0x84
MAX_STACK = 0xFF


# Win32 remote-call support.
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_EXECUTE_READWRITE = 0x40
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
STILL_ACTIVE = 259


# ---------------------------------------------------------------------------
# Proven AP world-pickup hooks (tested executable)
# ---------------------------------------------------------------------------

# Hook A intercepts the CanAcquireItem result. If vanilla says "cannot take"
# but the current RoomRecord is an AP-whitelisted physical pickup, it proceeds
# down the normal success path anyway.
PICKUP_HOOK_A_RVA = 0xEEB04
PICKUP_HOOK_A_SUCCESS_RVA = 0xEEB0C
PICKUP_HOOK_A_FAILURE_RVA = 0xEEB99
PICKUP_HOOK_A_ORIGINAL = bytes.fromhex(
    "84 C0 0F 84 8D 00 00 00"
)

# Hook B replaces the first CALL in acquisition case 4. AP-whitelisted
# physical pickups skip the entire vanilla reward implementation and branch
# directly to the existing CompleteItemAcquisition path.
PICKUP_HOOK_B_RVA = 0xEECC4
PICKUP_HOOK_B_RETURN_RVA = 0xEECC9
PICKUP_HOOK_B_AP_COMPLETE_RVA = 0xEED56
PICKUP_HOOK_B_ORIGINAL_CALL_RVA = 0xE5F90
PICKUP_HOOK_B_ORIGINAL = bytes.fromhex(
    "E8 C7 72 FF FF"
)

# Current pickup/controller fields used by the two tested hook sites.
CURRENT_ROOM_RECORD_INDEX_OFFSET = 0x6864
CURRENT_ITEM_ID_OFFSET = 0x6244

# RoomRecord fields used to ensure hooks only affect normal physical pickups.
ROOMREC_BEHAVIOR_FLAGS_OFFSET = 0x31
ROOMREC_G3_FLAG_OFFSET = 0x32
ROOMREC_ITEM_ID_OFFSET = 0x36

# Remote hook block layout.
PICKUP_HOOK_BLOCK_SIZE = 0x1000
PICKUP_WHITELIST_OFFSET = 0x000
PICKUP_HOOK_A_CAVE_OFFSET = 0x100
PICKUP_HOOK_B_CAVE_OFFSET = 0x300


# ---------------------------------------------------------------------------
# Runtime-confirmed gameplay-save tracking
# ---------------------------------------------------------------------------

# A confirmed typewriter save reaches this CALL exactly once; cancelling the
# save UI never reaches it. Immediately before the CALL the game has pushed a
# zero-based save-slot index (0..7), so at the callee boundary [ESP+4] is the
# selected slot. The original instruction is:
#   0043007F  E8 1C 03 00 00  CALL FUN_004303A0
SAVE_SLOT_SERIALIZE_CALL_RVA = 0x3007F
SAVE_SLOT_SERIALIZE_TARGET_RVA = 0x303A0
SAVE_SLOT_SERIALIZE_ORIGINAL = bytes.fromhex("E8 1C 03 00 00")

# FUN_0083AA50 is the SteamRemoteStorage::FileWrite("biodata.bin", ...)
# wrapper. The normal worker path calls it at 0083A4AE; FUN_0083A340 also has
# a synchronous path at 0083A3EE. Hook both CALL sites so the first writer
# completion after a captured slot save can atomically publish its result.
SAVE_WRITE_SYNC_CALL_RVA = 0x43A3EE
SAVE_WRITE_SYNC_ORIGINAL = bytes.fromhex("E8 5D 06 00 00")
SAVE_WRITE_ASYNC_CALL_RVA = 0x43A4AE
SAVE_WRITE_ASYNC_ORIGINAL = bytes.fromhex("E8 9D 05 00 00")
SAVE_WRITE_TARGET_RVA = 0x43AA50

# Runtime global used by the game's save state machine. +0x24 is busy and
# +0x28 is the completion/result code. Result 0 is success; nonzero results
# are treated by the game as errors (including restoring an Ink Ribbon).
SAVE_STORAGE_PTR_RVA = 0x9E5210
SAVE_STORAGE_RESULT_OFFSET = 0x28

# Shared remote tracking block. The Python client mirrors next_delivery_index
# into LIVE_CURSOR. The save-slot hook snapshots that cursor at the exact
# gameplay-save event, removing the normal polling race between AP delivery
# and a fast Steam write.
SAVE_TRACK_BLOCK_SIZE = 0x1000
SAVE_TRACK_LIVE_CURSOR_OFFSET = 0x00
SAVE_TRACK_CAPTURED_SLOT_OFFSET = 0x04
SAVE_TRACK_CAPTURED_CURSOR_OFFSET = 0x08
SAVE_TRACK_PENDING_OFFSET = 0x0C
SAVE_TRACK_REQUEST_SEQ_OFFSET = 0x10
SAVE_TRACK_COMPLETED_SLOT_OFFSET = 0x14
SAVE_TRACK_COMPLETED_CURSOR_OFFSET = 0x18
SAVE_TRACK_COMPLETED_RESULT_OFFSET = 0x1C
SAVE_TRACK_COMPLETED_SEQ_OFFSET = 0x20
# Runtime-confirmed load-side counterpart. FUN_0042F2F0 applies one selected
# staging save record back into live game state when [ESP+4] is 0..7. The
# same function is called with 10 to populate/refresh all save slots in the UI;
# that path must not trigger AP recovery. The first six bytes are preserved by
# the entry trampoline:
#   0042F2F0  8B 54 24 04  MOV EDX,[ESP+4]
#   0042F2F4  8B C2        MOV EAX,EDX
LOAD_SLOT_APPLY_RVA = 0x2F2F0
LOAD_SLOT_APPLY_ORIGINAL = bytes.fromhex("8B 54 24 04 8B C2")
LOAD_SLOT_APPLY_CONTINUE_RVA = 0x2F2F6

SAVE_TRACK_LOAD_SLOT_OFFSET = 0x24
SAVE_TRACK_LOAD_CURSOR_OFFSET = 0x28
SAVE_TRACK_LOAD_SEQ_OFFSET = 0x2C
# Separate global cursor used only by the load hook. SAVE_TRACK_LIVE_CURSOR is
# the cursor actually represented by the currently loaded RE1R state and may
# temporarily move backward while recovery is replaying post-save items. The
# global cursor never rewinds, so chained loads still recover through every AP
# item that had already been delivered before the first load.
SAVE_TRACK_GLOBAL_CURSOR_OFFSET = 0x30

# Runtime-confirmed session-start ordering:
#   Fresh New Game:             aGame ctor (FUN_00402DE0)
#   Existing save-slot load:    FUN_0042F2F0(slot 0..7) -> aGame ctor
# The selected-load hook sets a one-shot marker entirely in-process. The aGame
# entry hook consumes it; if no marker is present, it publishes a Fresh New
# Game event and snapshots the global AP delivery cursor. This avoids relying
# on Python polling between the two native calls.
SAVE_TRACK_AGAME_PENDING_LOAD_OFFSET = 0x34
SAVE_TRACK_FRESH_CURSOR_OFFSET = 0x38
SAVE_TRACK_FRESH_SEQ_OFFSET = 0x3C
SAVE_TRACK_DATA_SIZE = 0x40

# The client mirrors the server-authoritative set of checked randomized
# locations as a 64-byte Group-3 bitmask. The selected-slot load wrapper ORs
# this mask into the freshly loaded Group-3 bank immediately after vanilla
# FUN_0042F2F0 returns. FlagManager_SetFlag(group=3,id) is source-confirmed to
# perform exactly this OR operation, so bulk application is behavior-equivalent
# while avoiding up to 186 native calls during a load transition.
SAVE_TRACK_CHECKED_G3_MASK_OFFSET = 0x40
SAVE_TRACK_CHECKED_G3_MASK_SIZE = GROUP3_SIZE

SAVE_TRACK_SLOT_CAVE_OFFSET = 0x100
SAVE_TRACK_WRITER_CAVE_OFFSET = 0x200
SAVE_TRACK_LOAD_CAVE_OFFSET = 0x300
SAVE_TRACK_LOAD_TRAMPOLINE_OFFSET = 0x500
SAVE_TRACK_AGAME_CAVE_OFFSET = 0x600
SAVE_TRACK_NO_SLOT = 0xFFFFFFFF

# aGame constructor. Runtime tests established that a Fresh New Game reaches
# this constructor directly, whereas loading an existing save reaches the
# selected-slot apply function first and then this constructor. Preserve the
# first eight bytes:
#   00402DE0  56                 PUSH ESI
#   00402DE1  8B F1              MOV ESI,ECX
#   00402DE3  E8 98 E2 FF FF     CALL FUN_00401080
AGAME_CTOR_RVA = 0x2DE0
AGAME_CTOR_ORIGINAL = bytes.fromhex("56 8B F1 E8 98 E2 FF FF")
AGAME_CTOR_BASE_CALL_RVA = 0x1080
AGAME_CTOR_CONTINUE_RVA = 0x2DE8


# ---------------------------------------------------------------------------
# Location catalog
# ---------------------------------------------------------------------------

def load_location_catalog(path: Path) -> dict[int, str]:
    """
    Load the minimal:
        {
            "flag": "Unique AP location name",
            ...
        }
    JSON file.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))

    locations: dict[int, str] = {}
    names: set[str] = set()

    for raw_flag, raw_name in raw.items():
        flag = int(raw_flag)
        name = str(raw_name)

        if not 0 <= flag < GROUP3_FLAG_COUNT:
            raise ValueError(
                f"Location catalog contains invalid Group 3 flag {flag}"
            )

        if flag in locations:
            raise ValueError(f"Duplicate Group 3 flag in catalog: {flag}")

        if name in names:
            raise ValueError(f"Duplicate AP location name in catalog: {name!r}")

        locations[flag] = name
        names.add(name)

    if not locations:
        raise ValueError("Location catalog is empty.")

    return locations


# ---------------------------------------------------------------------------
# RE1R process / memory access
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short

    kernel32.VirtualAllocEx.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.VirtualAllocEx.restype = wintypes.LPVOID

    kernel32.VirtualFreeEx.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.DWORD,
    ]
    kernel32.VirtualFreeEx.restype = wintypes.BOOL

    kernel32.CreateRemoteThread.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CreateRemoteThread.restype = wintypes.HANDLE

    kernel32.WaitForSingleObject.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.VirtualProtectEx.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.VirtualProtectEx.restype = wintypes.BOOL

    kernel32.FlushInstructionCache.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        ctypes.c_size_t,
    ]
    kernel32.FlushInstructionCache.restype = wintypes.BOOL

    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
else:
    kernel32 = None
    user32 = None


class X86Builder:
    """Small purpose-built x86 rel32 emitter for the two pickup hooks."""

    def __init__(self, base_address: int) -> None:
        self.base = base_address
        self.buf = bytearray()
        self.labels: dict[str, int] = {}
        self.fixups: list[tuple[int, str]] = []

    @property
    def address(self) -> int:
        return self.base + len(self.buf)

    def emit(self, data: bytes) -> None:
        self.buf += data

    def label(self, name: str) -> None:
        if name in self.labels:
            raise ValueError(f"Duplicate x86 label {name!r}")
        self.labels[name] = self.address

    def _rel32_placeholder(self, opcode: bytes, label: str) -> None:
        self.emit(opcode)
        offset = len(self.buf)
        self.emit(b"\x00\x00\x00\x00")
        self.fixups.append((offset, label))

    def jmp_label(self, label: str) -> None:
        self._rel32_placeholder(b"\xE9", label)

    def jcc_label(self, condition_opcode: int, label: str) -> None:
        self._rel32_placeholder(bytes((0x0F, condition_opcode)), label)

    @staticmethod
    def _pack_rel32(displacement: int) -> bytes:
        return struct.pack("<I", displacement & 0xFFFFFFFF)

    def jmp_abs(self, target: int) -> None:
        self.emit(b"\xE9")
        next_ip = self.address + 4
        self.emit(self._pack_rel32(target - next_ip))

    def call_abs(self, target: int) -> None:
        self.emit(b"\xE8")
        next_ip = self.address + 4
        self.emit(self._pack_rel32(target - next_ip))

    def finish(self) -> bytes:
        for offset, label in self.fixups:
            if label not in self.labels:
                raise ValueError(f"Unknown x86 label {label!r}")

            target = self.labels[label]
            next_ip = self.base + offset + 4
            struct.pack_into(
                "<I",
                self.buf,
                offset,
                (target - next_ip) & 0xFFFFFFFF,
            )

        return bytes(self.buf)


def emit_current_pickup_is_whitelisted(
    builder: X86Builder,
    controller_reg: str,
    state_root_global: int,
    whitelist_address: int,
    no_label: str,
) -> None:
    """
    Fall through only if the active RoomRecord is a whitelisted Group-3
    physical pickup. The caller has PUSHAD active, so scratch registers are
    safe. EDI is the controller at Hook A; ESI is the controller at Hook B.
    """
    if controller_reg == "edi":
        builder.emit(
            b"\x8B\x87"
            + struct.pack("<I", CURRENT_ROOM_RECORD_INDEX_OFFSET)
        )  # mov eax,[edi+6864]
        cmp_item = (
            b"\x3B\x8F"
            + struct.pack("<I", CURRENT_ITEM_ID_OFFSET)
        )
    elif controller_reg == "esi":
        builder.emit(
            b"\x8B\x86"
            + struct.pack("<I", CURRENT_ROOM_RECORD_INDEX_OFFSET)
        )  # mov eax,[esi+6864]
        cmp_item = (
            b"\x3B\x8E"
            + struct.pack("<I", CURRENT_ITEM_ID_OFFSET)
        )
    else:
        raise ValueError(controller_reg)

    builder.emit(b"\x85\xC0")          # test eax,eax
    builder.jcc_label(0x88, no_label)    # js no

    builder.emit(
        b"\x8B\x15" + struct.pack("<I", state_root_global)
    )                                    # mov edx,[abs StateRoot]
    builder.emit(b"\x85\xD2")          # test edx,edx
    builder.jcc_label(0x84, no_label)    # jz no

    builder.emit(
        b"\x3B\x82" + struct.pack("<I", ROOM_RECORD_COUNT_OFFSET)
    )                                    # cmp eax,[edx+474]
    builder.jcc_label(0x83, no_label)    # jae no

    builder.emit(
        b"\x8B\x92" + struct.pack("<I", ROOM_RECORD_DATA_OFFSET)
    )                                    # mov edx,[edx+480]
    builder.emit(b"\x85\xD2")          # test edx,edx
    builder.jcc_label(0x84, no_label)    # jz no

    builder.emit(b"\x8B\x14\x82")      # mov edx,[edx+eax*4]
    builder.emit(b"\x85\xD2")          # test edx,edx
    builder.jcc_label(0x84, no_label)    # jz no

    builder.emit(
        b"\xF6\x42"
        + bytes((ROOMREC_BEHAVIOR_FLAGS_OFFSET,))
        + b"\x20"
    )                                    # test byte [edx+31],20
    builder.jcc_label(0x85, no_label)    # jnz no

    builder.emit(
        b"\x0F\xB7\x4A"
        + bytes((ROOMREC_ITEM_ID_OFFSET,))
    )                                    # movzx ecx,word [edx+36]
    builder.emit(cmp_item)               # cmp ecx,[controller+6244]
    builder.jcc_label(0x85, no_label)    # jne no

    builder.emit(
        b"\x0F\xB7\x42"
        + bytes((ROOMREC_G3_FLAG_OFFSET,))
    )                                    # movzx eax,word [edx+32]
    builder.emit(b"\x85\xC0")          # test eax,eax
    builder.jcc_label(0x84, no_label)    # jz no
    builder.emit(b"\x3D\x00\x02\x00\x00")  # cmp eax,512
    builder.jcc_label(0x83, no_label)    # jae no

    # BT treats the 64-byte whitelist as one 512-bit bit string.
    builder.emit(
        b"\x0F\xA3\x05" + struct.pack("<I", whitelist_address)
    )                                    # bt dword ptr [whitelist],eax
    builder.jcc_label(0x83, no_label)    # jnc no


def build_pickup_hook_a(
    cave_address: int,
    module_base: int,
    whitelist_address: int,
) -> bytes:
    builder = X86Builder(cave_address)

    # Preserve vanilla success immediately.
    builder.emit(b"\x84\xC0")          # test al,al
    builder.jcc_label(0x85, "success")  # jnz success

    builder.emit(b"\x60")              # pushad
    emit_current_pickup_is_whitelisted(
        builder,
        controller_reg="edi",
        state_root_global=module_base + STATE_ROOT_PTR_RVA,
        whitelist_address=whitelist_address,
        no_label="not_ap",
    )

    builder.emit(b"\x61")              # popad
    builder.jmp_abs(module_base + PICKUP_HOOK_A_SUCCESS_RVA)

    builder.label("not_ap")
    builder.emit(b"\x61")              # popad
    builder.jmp_abs(module_base + PICKUP_HOOK_A_FAILURE_RVA)

    builder.label("success")
    builder.jmp_abs(module_base + PICKUP_HOOK_A_SUCCESS_RVA)

    return builder.finish()


def build_pickup_hook_b(
    cave_address: int,
    module_base: int,
    whitelist_address: int,
) -> bytes:
    builder = X86Builder(cave_address)
    builder.emit(b"\x60")              # pushad

    emit_current_pickup_is_whitelisted(
        builder,
        controller_reg="esi",
        state_root_global=module_base + STATE_ROOT_PTR_RVA,
        whitelist_address=whitelist_address,
        no_label="vanilla",
    )

    # AP location: preserve the game's own completion path, but skip every
    # vanilla reward branch in acquisition case 4.
    builder.emit(b"\x61")              # popad
    builder.jmp_abs(module_base + PICKUP_HOOK_B_AP_COMPLETE_RVA)

    builder.label("vanilla")
    builder.emit(b"\x61")              # popad
    builder.call_abs(module_base + PICKUP_HOOK_B_ORIGINAL_CALL_RVA)
    builder.jmp_abs(module_base + PICKUP_HOOK_B_RETURN_RVA)

    return builder.finish()


def build_group3_whitelist(flags: set[int]) -> bytes:
    data = bytearray(GROUP3_SIZE)

    for flag in flags:
        if not 0 <= flag < GROUP3_FLAG_COUNT:
            raise ValueError(f"Invalid Group-3 whitelist flag {flag}")
        data[flag >> 3] |= 1 << (flag & 7)

    return bytes(data)


def make_rel32_jmp(source: int, target: int, total_length: int) -> bytes:
    if total_length < 5:
        raise ValueError("x86 JMP patch must be at least 5 bytes")

    displacement = target - (source + 5)
    return (
        b"\xE9"
        + struct.pack("<I", displacement & 0xFFFFFFFF)
        + b"\x90" * (total_length - 5)
    )


def make_rel32_call(source: int, target: int) -> bytes:
    displacement = target - (source + 5)
    return b"\xE8" + struct.pack("<I", displacement & 0xFFFFFFFF)


def build_save_slot_capture_cave(
    cave_address: int,
    module_base: int,
    data_base: int,
) -> bytes:
    """Capture save slot + AP delivery cursor, then tail-jump to vanilla."""
    builder = X86Builder(cave_address)

    # Patched CALL enters with:
    #   [ESP]   = vanilla return address (00430084)
    #   [ESP+4] = zero-based RE1R save slot
    # Preserve EAX while recording the event.
    builder.emit(b"\x50")                                  # push eax
    builder.emit(b"\x8B\x44\x24\x08")                  # mov eax,[esp+8]
    builder.emit(
        b"\xA3" + struct.pack("<I", data_base + SAVE_TRACK_CAPTURED_SLOT_OFFSET)
    )                                                         # mov [slot],eax
    builder.emit(
        b"\xA1" + struct.pack("<I", data_base + SAVE_TRACK_LIVE_CURSOR_OFFSET)
    )                                                         # mov eax,[live_cursor]
    builder.emit(
        b"\xA3" + struct.pack("<I", data_base + SAVE_TRACK_CAPTURED_CURSOR_OFFSET)
    )                                                         # mov [captured_cursor],eax
    builder.emit(
        b"\xC7\x05"
        + struct.pack("<I", data_base + SAVE_TRACK_PENDING_OFFSET)
        + struct.pack("<I", 1)
    )                                                         # pending=1
    builder.emit(
        b"\xFF\x05"
        + struct.pack("<I", data_base + SAVE_TRACK_REQUEST_SEQ_OFFSET)
    )                                                         # ++request_seq
    builder.emit(b"\x58")                                  # pop eax

    # Tail-jump rather than CALL: this preserves the original return address
    # and [ESP+4] argument exactly as FUN_004303A0 expects them.
    builder.jmp_abs(module_base + SAVE_SLOT_SERIALIZE_TARGET_RVA)
    return builder.finish()


def build_save_writer_wrapper_cave(
    cave_address: int,
    module_base: int,
    data_base: int,
) -> bytes:
    """Call vanilla FileWrite wrapper and publish the first pending result."""
    builder = X86Builder(cave_address)

    # Both patched writer CALL sites already have ECX set exactly as vanilla
    # expects. Call the original writer before touching registers.
    builder.call_abs(module_base + SAVE_WRITE_TARGET_RVA)
    builder.emit(b"\x50")                                  # push eax

    builder.emit(
        b"\x83\x3D"
        + struct.pack("<I", data_base + SAVE_TRACK_PENDING_OFFSET)
        + b"\x00"
    )                                                         # cmp [pending],0
    builder.jcc_label(0x84, "done")                           # je done

    builder.emit(
        b"\xA1" + struct.pack("<I", module_base + SAVE_STORAGE_PTR_RVA)
    )                                                         # mov eax,[storage*]
    builder.emit(b"\x85\xC0")                            # test eax,eax
    builder.jcc_label(0x84, "missing_storage")                # jz missing
    builder.emit(
        b"\x8B\x40" + bytes((SAVE_STORAGE_RESULT_OFFSET,))
    )                                                         # mov eax,[eax+28]
    builder.jmp_label("have_result")

    builder.label("missing_storage")
    builder.emit(b"\xB8\xFF\xFF\xFF\xFF")          # mov eax,FFFFFFFF

    builder.label("have_result")
    builder.emit(
        b"\xA3" + struct.pack("<I", data_base + SAVE_TRACK_COMPLETED_RESULT_OFFSET)
    )
    builder.emit(
        b"\xA1" + struct.pack("<I", data_base + SAVE_TRACK_CAPTURED_SLOT_OFFSET)
    )
    builder.emit(
        b"\xA3" + struct.pack("<I", data_base + SAVE_TRACK_COMPLETED_SLOT_OFFSET)
    )
    builder.emit(
        b"\xA1" + struct.pack("<I", data_base + SAVE_TRACK_CAPTURED_CURSOR_OFFSET)
    )
    builder.emit(
        b"\xA3" + struct.pack("<I", data_base + SAVE_TRACK_COMPLETED_CURSOR_OFFSET)
    )
    builder.emit(
        b"\xFF\x05"
        + struct.pack("<I", data_base + SAVE_TRACK_COMPLETED_SEQ_OFFSET)
    )                                                         # ++completed_seq
    builder.emit(
        b"\xC7\x05"
        + struct.pack("<I", data_base + SAVE_TRACK_PENDING_OFFSET)
        + struct.pack("<I", 0)
    )                                                         # pending=0

    builder.label("done")
    builder.emit(b"\x58")                                  # pop eax
    builder.emit(b"\xC3")                                  # ret
    return builder.finish()


def build_load_slot_original_trampoline(
    trampoline_address: int,
    module_base: int,
) -> bytes:
    """Re-execute the six displaced load-function bytes and continue vanilla."""
    builder = X86Builder(trampoline_address)
    builder.emit(LOAD_SLOT_APPLY_ORIGINAL)
    builder.jmp_abs(module_base + LOAD_SLOT_APPLY_CONTINUE_RVA)
    return builder.finish()


def build_load_slot_capture_cave(
    cave_address: int,
    trampoline_address: int,
    module_base: int,
    data_base: int,
) -> bytes:
    """
    Wrap real FUN_0042F2F0 slot loads (0..7).

    For a selected slot, duplicate the original stack argument and CALL a
    trampoline containing the displaced entry bytes. This lets the entire
    vanilla function return to this wrapper while leaving the original caller
    frame untouched. We can then OR the AP-checked Group-3 mask into the newly
    loaded StateRoot before returning to the original caller.

    Arguments 9/10 remain pure vanilla tail-jumps: 9 is a special path and 10
    is the all-slot save-list/UI population path.
    """
    builder = X86Builder(cave_address)

    # Match the displaced entry semantics for the branch decision.
    builder.emit(LOAD_SLOT_APPLY_ORIGINAL)                  # mov edx,[esp+4]; mov eax,edx
    builder.emit(b"\x83\xFA\x08")                        # cmp edx,8
    builder.jcc_label(0x83, "passthrough")                 # jae passthrough

    # Publish semantic load event + pre-load global AP delivery cursor.
    builder.emit(
        b"\x89\x15" + struct.pack("<I", data_base + SAVE_TRACK_LOAD_SLOT_OFFSET)
    )                                                       # mov [load_slot],edx
    builder.emit(
        b"\xA1" + struct.pack("<I", data_base + SAVE_TRACK_GLOBAL_CURSOR_OFFSET)
    )                                                       # mov eax,[global_cursor]
    builder.emit(
        b"\xA3" + struct.pack("<I", data_base + SAVE_TRACK_LOAD_CURSOR_OFFSET)
    )                                                       # mov [load_cursor],eax
    builder.emit(
        b"\xFF\x05"
        + struct.pack("<I", data_base + SAVE_TRACK_LOAD_SEQ_OFFSET)
    )                                                       # ++load_seq
    builder.emit(
        b"\xC7\x05"
        + struct.pack("<I", data_base + SAVE_TRACK_AGAME_PENDING_LOAD_OFFSET)
        + struct.pack("<I", 1)
    )                                                       # next aGame belongs to load

    # Duplicate param_2 on a temporary callee frame. The trampoline/function
    # consumes this duplicate with its normal thiscall RET 4, leaving the
    # original [return,param_2] frame intact for our final RET 4 below.
    builder.emit(b"\xFF\x74\x24\x04")                    # push dword ptr [esp+4]
    builder.call_abs(trampoline_address)

    # Preserve the vanilla return register/flags while bulk-applying the AP
    # checked-location mask to the just-restored Group-3 bank.
    builder.emit(b"\x9C")                                  # pushfd
    builder.emit(b"\x60")                                  # pushad
    builder.emit(
        b"\x8B\x3D" + struct.pack("<I", module_base + STATE_ROOT_PTR_RVA)
    )                                                       # mov edi,[StateRoot*]
    builder.emit(b"\x85\xFF")                            # test edi,edi
    builder.jcc_label(0x84, "restore_done")                # jz restore_done
    builder.emit(b"\x81\xC7" + struct.pack("<I", GROUP3_OFFSET))
                                                            # add edi,GROUP3_OFFSET
    builder.emit(
        b"\xBE" + struct.pack("<I", data_base + SAVE_TRACK_CHECKED_G3_MASK_OFFSET)
    )                                                       # mov esi,mask
    builder.emit(b"\xB9" + struct.pack("<I", GROUP3_SIZE // 4))
                                                            # mov ecx,16

    builder.label("restore_loop")
    builder.emit(b"\x8B\x06")                            # mov eax,[esi]
    builder.emit(b"\x09\x07")                            # or [edi],eax
    builder.emit(b"\x83\xC6\x04")                      # add esi,4
    builder.emit(b"\x83\xC7\x04")                      # add edi,4
    builder.emit(b"\x49")                                  # dec ecx
    builder.jcc_label(0x85, "restore_loop")                # jnz restore_loop

    builder.label("restore_done")
    builder.emit(b"\x61")                                  # popad
    builder.emit(b"\x9D")                                  # popfd
    builder.emit(b"\xC2\x04\x00")                      # ret 4

    # Non-selected load modes must behave exactly as v6.5 did: execute the
    # displaced bytes once and continue in vanilla without post-processing.
    builder.label("passthrough")
    builder.jmp_abs(module_base + LOAD_SLOT_APPLY_CONTINUE_RVA)
    return builder.finish()


def build_agame_session_cave(
    cave_address: int,
    module_base: int,
    data_base: int,
) -> bytes:
    """
    Preserve the aGame constructor prologue and publish Fresh New Game events.

    A selected-slot load sets SAVE_TRACK_AGAME_PENDING_LOAD before vanilla
    FUN_0042F2F0 returns. Runtime-confirmed ordering is load-slot apply first,
    then this constructor. Consume that marker without publishing a fresh event.
    If no marker exists, this constructor is a semantic Fresh New Game start.
    """
    builder = X86Builder(cave_address)

    builder.emit(b"\x50")                                  # push eax
    builder.emit(
        b"\x83\x3D"
        + struct.pack("<I", data_base + SAVE_TRACK_AGAME_PENDING_LOAD_OFFSET)
        + b"\x00"
    )                                                       # cmp [pending_load],0
    builder.jcc_label(0x85, "loaded_session")              # jne loaded_session

    builder.emit(
        b"\xA1" + struct.pack("<I", data_base + SAVE_TRACK_GLOBAL_CURSOR_OFFSET)
    )                                                       # mov eax,[global_cursor]
    builder.emit(
        b"\xA3" + struct.pack("<I", data_base + SAVE_TRACK_FRESH_CURSOR_OFFSET)
    )                                                       # mov [fresh_cursor],eax
    builder.emit(
        b"\xFF\x05" + struct.pack("<I", data_base + SAVE_TRACK_FRESH_SEQ_OFFSET)
    )                                                       # ++fresh_seq
    builder.jmp_label("classification_done")

    builder.label("loaded_session")
    builder.emit(
        b"\xC7\x05"
        + struct.pack("<I", data_base + SAVE_TRACK_AGAME_PENDING_LOAD_OFFSET)
        + struct.pack("<I", 0)
    )                                                       # consume marker

    builder.label("classification_done")
    builder.emit(b"\x58")                                  # pop eax

    # Reproduce the displaced constructor prologue exactly, except that the
    # original relative CALL is re-emitted against its absolute target.
    builder.emit(b"\x56")                                  # push esi
    builder.emit(b"\x8B\xF1")                            # mov esi,ecx
    builder.call_abs(module_base + AGAME_CTOR_BASE_CALL_RVA)
    builder.jmp_abs(module_base + AGAME_CTOR_CONTINUE_RVA)
    return builder.finish()


class RE1RMemory:
    def __init__(self) -> None:
        self.pm: Optional[pymem.Pymem] = None
        self.module_base: int = 0

        self.pickup_hooks_installed = False
        self.pickup_hook_flags: frozenset[int] = frozenset()
        self._pickup_hook_remote_base = 0
        self._pickup_hook_patch_a = b""
        self._pickup_hook_patch_b = b""

        self.save_tracking_hooks_installed = False
        self._save_track_remote_base = 0
        self._save_track_slot_patch = b""
        self._save_track_sync_patch = b""
        self._save_track_async_patch = b""
        self._save_track_load_patch = b""
        self._save_track_agame_patch = b""

    @property
    def attached(self) -> bool:
        return self.pm is not None and self.module_base != 0

    def process_alive(self) -> bool:
        if self.pm is None or kernel32 is None:
            return False

        exit_code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(
            wintypes.HANDLE(self.pm.process_handle),
            ctypes.byref(exit_code),
        )
        return bool(ok and exit_code.value == STILL_ACTIVE)

    def _forget_pickup_hook_state(self) -> None:
        self.pickup_hooks_installed = False
        self.pickup_hook_flags = frozenset()
        self._pickup_hook_remote_base = 0
        self._pickup_hook_patch_a = b""
        self._pickup_hook_patch_b = b""

    def _forget_save_tracking_hook_state(self) -> None:
        self.save_tracking_hooks_installed = False
        self._save_track_remote_base = 0
        self._save_track_slot_patch = b""
        self._save_track_sync_patch = b""
        self._save_track_async_patch = b""
        self._save_track_load_patch = b""
        self._save_track_agame_patch = b""

    def detach(self) -> None:
        if self.pm is not None:
            if self.process_alive() and self.save_tracking_hooks_installed:
                try:
                    self.restore_save_tracking_hooks()
                except Exception:
                    logger.exception(
                        "RE1R: failed to restore save-tracking hooks while "
                        "detaching. Close bhd.exe before continuing if the "
                        "game is still running."
                    )

            if self.process_alive() and self.pickup_hooks_installed:
                try:
                    self.restore_pickup_hooks()
                except Exception:
                    logger.exception(
                        "RE1R: failed to restore pickup hooks while detaching. "
                        "Close bhd.exe before continuing if the game is still "
                        "running."
                    )

            try:
                self.pm.close_process()
            except Exception:
                pass

        # If the process exited, Windows already reclaimed remote blocks.
        self._forget_save_tracking_hook_state()
        self._forget_pickup_hook_state()
        self.pm = None
        self.module_base = 0

    def attach(self) -> bool:
        if self.attached:
            return True

        try:
            pm = pymem.Pymem(PROCESS_NAME)
            module = pymem.process.module_from_name(
                pm.process_handle,
                PROCESS_NAME,
            )

            if module is None:
                try:
                    pm.close_process()
                except Exception:
                    pass
                return False

            self.pm = pm
            self.module_base = module.lpBaseOfDll
            return True

        except Exception:
            self.detach()
            return False

    def read_uint(self, address: int) -> Optional[int]:
        if self.pm is None:
            return None

        try:
            return self.pm.read_uint(address)
        except Exception:
            return None

    def get_state_root(self) -> Optional[int]:
        if not self.attached:
            return None

        state_root = self.read_uint(
            self.module_base + STATE_ROOT_PTR_RVA
        )

        if not state_root:
            return None

        return state_root

    def gameplay_loaded(self) -> tuple[bool, Optional[int]]:
        """
        Gate location scanning on the runtime RoomRecord array.

        Observed:
          title/save-select/loading: count=0, capacity=0, data=NULL
          playable/pause:           count>0, capacity>=count, data!=NULL

        A room transition may temporarily make this false. That is fine:
        the scanner pauses and performs a full persistent-flag resync when
        the next room finishes loading.
        """
        state_root = self.get_state_root()
        if state_root is None:
            return False, None

        count = self.read_uint(
            state_root + ROOM_RECORD_COUNT_OFFSET
        )
        capacity = self.read_uint(
            state_root + ROOM_RECORD_CAPACITY_OFFSET
        )
        data = self.read_uint(
            state_root + ROOM_RECORD_DATA_OFFSET
        )

        if count is None or capacity is None or data is None:
            return False, state_root

        loaded = (
            data != 0
            and count > 0
            and count <= capacity
            and capacity < 0x10000
        )

        return loaded, state_root

    def read_group3(self, state_root: int) -> Optional[bytes]:
        if self.pm is None:
            return None

        try:
            data = self.pm.read_bytes(
                state_root + GROUP3_OFFSET,
                GROUP3_SIZE,
            )
        except Exception:
            return None

        if len(data) != GROUP3_SIZE:
            return None

        return data

    def or_group3_mask(self, state_root: int, mask: bytes) -> bool:
        """OR a 64-byte randomized-location mask into the live Group-3 bank."""
        if self.pm is None or len(mask) != GROUP3_SIZE:
            return False
        current = self.read_group3(state_root)
        if current is None:
            return False
        merged = bytes(a | b for a, b in zip(current, mask))
        if merged == current:
            return True
        try:
            address = state_root + GROUP3_OFFSET
            self.pm.write_bytes(address, merged, len(merged))
            return self.pm.read_bytes(address, len(merged)) == merged
        except Exception:
            return False

    def get_context(self) -> Optional[int]:
        if not self.attached:
            return None

        context = self.read_uint(
            self.module_base + CONTEXT_PTR_RVA
        )

        return context or None

    def get_primary_stage_room(self) -> Optional[tuple[int, int]]:
        """
        Read the runtime-confirmed primary stage/room pair from Context.
        """
        context = self.get_context()
        if not context:
            return None

        stage = self.read_uint(context + CONTEXT_STAGE_OFFSET)
        room = self.read_uint(context + CONTEXT_ROOM_OFFSET)

        if stage is None or room is None:
            return None

        return stage, room

    def virtual_itembox_room_active(self) -> bool:
        stage_room = self.get_primary_stage_room()
        return stage_room == (
            VIRTUAL_ITEMBOX_STAGE,
            VIRTUAL_ITEMBOX_ROOM,
        )

    def aending_active(self) -> bool:
        """
        Return True while an instantiated aEnding object is in the active
        sArea state stack.

        This is read-only and intentionally validates both owner vtables before
        following the chain. Failed reads or unexpected pointers are treated as
        "not active" rather than as victory.
        """
        if not self.attached:
            return False

        root = self.read_uint(self.module_base + SBHDMAIN_PTR_RVA)
        if not root:
            return False

        root_vtable = self.read_uint(root)
        if root_vtable != self.module_base + SBHDMAIN_VTABLE_RVA:
            return False

        manager = self.read_uint(root + SBHDMAIN_AREA_MANAGER_OFFSET)
        if not manager:
            return False

        manager_vtable = self.read_uint(manager)
        if manager_vtable != self.module_base + AREA_MANAGER_VTABLE_RVA:
            return False

        count = self.read_uint(manager + AREA_ACTIVE_COUNT_OFFSET)
        if count is None or count > AREA_MAX_ACTIVE_STATES:
            return False

        ending_vtable = self.module_base + AENDING_VTABLE_RVA

        for index in range(count):
            state = self.read_uint(
                manager + AREA_ACTIVE_STATES_OFFSET + index * 4
            )
            if not state:
                continue

            if self.read_uint(state) == ending_vtable:
                return True

        return False

    def get_active_agame(self) -> Optional[int]:
        """
        Return the active aGame object from the runtime-confirmed sArea stack.

        Owner vtables and active-count bounds are validated before following
        state pointers. This is used only as a guard for native Item Box UI
        invocation and AP delivery timing.
        """
        if not self.attached:
            return None

        root = self.read_uint(self.module_base + SBHDMAIN_PTR_RVA)
        if not root:
            return None

        if self.read_uint(root) != self.module_base + SBHDMAIN_VTABLE_RVA:
            return None

        manager = self.read_uint(root + SBHDMAIN_AREA_MANAGER_OFFSET)
        if not manager:
            return None

        if self.read_uint(manager) != self.module_base + AREA_MANAGER_VTABLE_RVA:
            return None

        count = self.read_uint(manager + AREA_ACTIVE_COUNT_OFFSET)
        if count is None or count > AREA_MAX_ACTIVE_STATES:
            return None

        agame_vtable = self.module_base + AGAME_VTABLE_RVA

        for index in range(count):
            state = self.read_uint(
                manager + AREA_ACTIVE_STATES_OFFSET + index * 4
            )
            if state and self.read_uint(state) == agame_vtable:
                return state

        return None

    def ordinary_gameplay_active(self) -> bool:
        agame = self.get_active_agame()
        if not agame:
            return False

        mode = self.read_uint(agame + AGAME_CURRENT_MODE_OFFSET)
        return mode == AGAME_NORMAL_GAMEPLAY_MODE

    def read_slots(
        self,
        base: int,
        count: int,
    ) -> Optional[list[tuple[int, int]]]:
        if self.pm is None:
            return None

        try:
            raw = self.pm.read_bytes(base, count * SLOT_SIZE)
        except Exception:
            return None

        if len(raw) != count * SLOT_SIZE:
            return None

        return [
            struct.unpack_from("<ii", raw, slot * SLOT_SIZE)
            for slot in range(count)
        ]

    def read_inventory(
        self,
        context: int,
    ) -> Optional[list[tuple[int, int]]]:
        return self.read_slots(
            context + INVENTORY_SLOTS_OFFSET,
            JILL_INVENTORY_SLOTS,
        )

    def read_itembox(
        self,
        context: int,
    ) -> Optional[list[tuple[int, int]]]:
        return self.read_slots(
            context
            + ITEMBOX_BASE_OFFSET
            + ITEMBOX_BANK * ITEMBOX_BANK_STRIDE,
            ITEMBOX_SLOTS,
        )

    def item_flags(
        self,
        item_id: int,
    ) -> Optional[int]:
        if not self.attached:
            return None

        if not 1 <= item_id <= MAX_GAME_ITEM_ID:
            return None

        return self.read_uint(
            self.module_base
            + ITEM_TABLE_RVA
            + item_id * ITEM_RECORD_SIZE
            + ITEM_FLAGS_OFFSET
        )

    def item_is_stackable(
        self,
        item_id: int,
    ) -> Optional[bool]:
        flags = self.item_flags(item_id)
        if flags is None:
            return None

        return bool(flags & ITEM_FLAG_STACKABLE)

    @staticmethod
    def _push32(value: int) -> bytes:
        return b"\x68" + struct.pack("<I", value & 0xFFFFFFFF)

    @classmethod
    def _make_thiscall4_stub(
        cls,
        this_ptr: int,
        function: int,
        arg1: int,
        arg2: int,
        arg3: int,
        arg4: int,
    ) -> bytes:
        # 32-bit MSVC __thiscall:
        #   ECX = this
        #   remaining args pushed right-to-left.
        code = bytearray()
        code += b"\x55"                                  # push ebp
        code += b"\x89\xE5"                             # mov ebp, esp
        code += b"\xB9" + struct.pack("<I", this_ptr)  # mov ecx, this
        code += cls._push32(arg4)
        code += cls._push32(arg3)
        code += cls._push32(arg2)
        code += cls._push32(arg1)
        code += b"\xB8" + struct.pack("<I", function)  # mov eax, fn
        code += b"\xFF\xD0"                             # call eax

        # Restore the remote thread's entry stack regardless of the target
        # function's cleanup convention.
        code += b"\x89\xEC"                             # mov esp, ebp
        code += b"\x5D"                                  # pop ebp
        code += b"\x31\xC0"                             # xor eax, eax
        code += b"\xC2\x04\x00"                        # ret 4
        return bytes(code)

    @classmethod
    def _make_thiscall6_stub(
        cls,
        this_ptr: int,
        function: int,
        arg1: int,
        arg2: int,
        arg3: int,
        arg4: int,
        arg5: int,
        arg6: int,
    ) -> bytes:
        # 32-bit MSVC __thiscall with six explicit stack arguments.
        code = bytearray()
        code += b"\x55"                                  # push ebp
        code += b"\x89\xE5"                             # mov ebp, esp
        code += b"\xB9" + struct.pack("<I", this_ptr)  # mov ecx, this
        code += cls._push32(arg6)
        code += cls._push32(arg5)
        code += cls._push32(arg4)
        code += cls._push32(arg3)
        code += cls._push32(arg2)
        code += cls._push32(arg1)
        code += b"\xB8" + struct.pack("<I", function)  # mov eax, fn
        code += b"\xFF\xD0"                             # call eax
        code += b"\x89\xEC"                             # mov esp, ebp
        code += b"\x5D"                                  # pop ebp
        code += b"\x31\xC0"                             # xor eax, eax
        code += b"\xC2\x04\x00"                        # ret 4
        return bytes(code)

    @classmethod
    def _make_open_itembox_stub(
        cls,
        context: int,
        ui_manager: int,
        clear_flag_function: int,
        open_ui_function: int,
    ) -> bytes:
        """
        Build the exact two-call vanilla sequence observed when a physical
        Item Box opens, without passing or constructing a physical-box object.
        """
        code = bytearray()
        code += b"\x55"                                  # push ebp
        code += b"\x89\xE5"                             # mov ebp, esp

        # FUN_0048B740(Context, 7): clear Context+0x30 bit 7.
        code += b"\xB9" + struct.pack("<I", context)
        code += cls._push32(ITEMBOX_OPEN_CLEAR_FLAG)
        code += b"\xB8" + struct.pack("<I", clear_flag_function)
        code += b"\xFF\xD0"

        # FUN_004939E0(UIManager, 5, 0, 0, 0, 0, 1): Item Box UI.
        code += b"\xB9" + struct.pack("<I", ui_manager)
        code += cls._push32(1)
        code += cls._push32(0)
        code += cls._push32(0)
        code += cls._push32(0)
        code += cls._push32(0)
        code += cls._push32(5)
        code += b"\xB8" + struct.pack("<I", open_ui_function)
        code += b"\xFF\xD0"

        code += b"\x89\xEC"
        code += b"\x5D"
        code += b"\x31\xC0"
        code += b"\xC2\x04\x00"
        return bytes(code)

    def _remote_call(
        self,
        stub: bytes,
        timeout_ms: int = 5000,
    ) -> None:
        if self.pm is None or kernel32 is None:
            raise RuntimeError(
                "Native RE1R item delivery requires Windows and bhd.exe."
            )

        process = wintypes.HANDLE(self.pm.process_handle)

        remote = kernel32.VirtualAllocEx(
            process,
            None,
            len(stub),
            MEM_COMMIT | MEM_RESERVE,
            PAGE_EXECUTE_READWRITE,
        )

        if not remote:
            raise ctypes.WinError(ctypes.get_last_error())

        remote_addr = ctypes.cast(
            remote,
            ctypes.c_void_p,
        ).value

        thread = None
        completed = False

        try:
            self.pm.write_bytes(
                remote_addr,
                stub,
                len(stub),
            )

            thread_id = wintypes.DWORD()

            thread = kernel32.CreateRemoteThread(
                process,
                None,
                0,
                remote,
                None,
                0,
                ctypes.byref(thread_id),
            )

            if not thread:
                raise ctypes.WinError(ctypes.get_last_error())

            result = kernel32.WaitForSingleObject(
                thread,
                timeout_ms,
            )

            if result == WAIT_TIMEOUT:
                # Do not free code that a still-running thread may execute.
                raise TimeoutError(
                    "RE1R native setter call timed out. "
                    f"Remote stub left allocated at 0x{remote_addr:08X}."
                )

            if result != WAIT_OBJECT_0:
                raise RuntimeError(
                    "WaitForSingleObject returned "
                    f"0x{result:08X}."
                )

            completed = True

        finally:
            if thread:
                kernel32.CloseHandle(thread)

            if completed:
                kernel32.VirtualFreeEx(
                    process,
                    remote,
                    0,
                    MEM_RELEASE,
                )

    def _protect_and_write_code(
        self,
        address: int,
        data: bytes,
    ) -> None:
        if self.pm is None or kernel32 is None:
            raise RuntimeError("RE1R process is not attached")

        process = wintypes.HANDLE(self.pm.process_handle)
        old_protection = wintypes.DWORD()

        if not kernel32.VirtualProtectEx(
            process,
            ctypes.c_void_p(address),
            len(data),
            PAGE_EXECUTE_READWRITE,
            ctypes.byref(old_protection),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        try:
            self.pm.write_bytes(address, data, len(data))

            verify = self.pm.read_bytes(address, len(data))
            if verify != data:
                raise RuntimeError(
                    f"RE1R code-patch verification failed at 0x{address:08X}"
                )

            if not kernel32.FlushInstructionCache(
                process,
                ctypes.c_void_p(address),
                len(data),
            ):
                raise ctypes.WinError(ctypes.get_last_error())

        finally:
            ignored = wintypes.DWORD()
            kernel32.VirtualProtectEx(
                process,
                ctypes.c_void_p(address),
                len(data),
                old_protection.value,
                ctypes.byref(ignored),
            )

    def _free_save_tracking_hook_block(self) -> None:
        if (
            self.pm is None
            or kernel32 is None
            or not self._save_track_remote_base
        ):
            self._save_track_remote_base = 0
            return

        process = wintypes.HANDLE(self.pm.process_handle)
        if not kernel32.VirtualFreeEx(
            process,
            ctypes.c_void_p(self._save_track_remote_base),
            0,
            MEM_RELEASE,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        self._save_track_remote_base = 0

    def install_save_tracking_hooks(
        self,
        live_cursor: int,
        checked_group3_mask: bytes,
    ) -> None:
        """Install save/load/New-Game tracking plus checked-location restoration."""
        if self.pm is None or kernel32 is None or not self.attached:
            raise RuntimeError("Cannot install save-tracking hooks while detached")

        if self.save_tracking_hooks_installed:
            self.write_tracking_cursors(live_cursor, live_cursor)
            self.write_checked_group3_mask(checked_group3_mask)
            return

        slot_call = self.module_base + SAVE_SLOT_SERIALIZE_CALL_RVA
        sync_call = self.module_base + SAVE_WRITE_SYNC_CALL_RVA
        async_call = self.module_base + SAVE_WRITE_ASYNC_CALL_RVA
        load_entry = self.module_base + LOAD_SLOT_APPLY_RVA
        agame_entry = self.module_base + AGAME_CTOR_RVA

        expected = (
            (slot_call, SAVE_SLOT_SERIALIZE_ORIGINAL, "save-slot CALL"),
            (sync_call, SAVE_WRITE_SYNC_ORIGINAL, "sync writer CALL"),
            (async_call, SAVE_WRITE_ASYNC_ORIGINAL, "async writer CALL"),
            (load_entry, LOAD_SLOT_APPLY_ORIGINAL, "load-slot entry"),
            (agame_entry, AGAME_CTOR_ORIGINAL, "aGame constructor entry"),
        )
        for address, original, label in expected:
            actual = self.pm.read_bytes(address, len(original))
            if actual != original:
                raise RuntimeError(
                    f"{label} bytes do not match the tested bhd.exe build: "
                    f"got {actual.hex(' ').upper()}"
                )

        process = wintypes.HANDLE(self.pm.process_handle)
        remote = kernel32.VirtualAllocEx(
            process,
            None,
            SAVE_TRACK_BLOCK_SIZE,
            MEM_COMMIT | MEM_RESERVE,
            PAGE_EXECUTE_READWRITE,
        )
        if not remote:
            raise ctypes.WinError(ctypes.get_last_error())

        remote_base = ctypes.cast(remote, ctypes.c_void_p).value
        assert remote_base is not None

        data_base = remote_base
        slot_cave_addr = remote_base + SAVE_TRACK_SLOT_CAVE_OFFSET
        writer_cave_addr = remote_base + SAVE_TRACK_WRITER_CAVE_OFFSET
        load_cave_addr = remote_base + SAVE_TRACK_LOAD_CAVE_OFFSET
        load_trampoline_addr = remote_base + SAVE_TRACK_LOAD_TRAMPOLINE_OFFSET
        agame_cave_addr = remote_base + SAVE_TRACK_AGAME_CAVE_OFFSET

        slot_cave = build_save_slot_capture_cave(
            slot_cave_addr, self.module_base, data_base
        )
        writer_cave = build_save_writer_wrapper_cave(
            writer_cave_addr, self.module_base, data_base
        )
        load_trampoline = build_load_slot_original_trampoline(
            load_trampoline_addr, self.module_base
        )
        load_cave = build_load_slot_capture_cave(
            load_cave_addr, load_trampoline_addr, self.module_base, data_base
        )
        agame_cave = build_agame_session_cave(
            agame_cave_addr, self.module_base, data_base
        )

        if len(slot_cave) >= SAVE_TRACK_WRITER_CAVE_OFFSET - SAVE_TRACK_SLOT_CAVE_OFFSET:
            kernel32.VirtualFreeEx(process, remote, 0, MEM_RELEASE)
            raise RuntimeError("Save-slot tracking cave is unexpectedly too large")
        if len(writer_cave) >= SAVE_TRACK_LOAD_CAVE_OFFSET - SAVE_TRACK_WRITER_CAVE_OFFSET:
            kernel32.VirtualFreeEx(process, remote, 0, MEM_RELEASE)
            raise RuntimeError("Save-writer tracking cave is unexpectedly too large")
        if len(load_cave) >= SAVE_TRACK_LOAD_TRAMPOLINE_OFFSET - SAVE_TRACK_LOAD_CAVE_OFFSET:
            kernel32.VirtualFreeEx(process, remote, 0, MEM_RELEASE)
            raise RuntimeError("Load-slot tracking cave is unexpectedly too large")
        if len(load_trampoline) >= SAVE_TRACK_AGAME_CAVE_OFFSET - SAVE_TRACK_LOAD_TRAMPOLINE_OFFSET:
            kernel32.VirtualFreeEx(process, remote, 0, MEM_RELEASE)
            raise RuntimeError("Load-slot trampoline is unexpectedly too large")
        if len(agame_cave) >= SAVE_TRACK_BLOCK_SIZE - SAVE_TRACK_AGAME_CAVE_OFFSET:
            kernel32.VirtualFreeEx(process, remote, 0, MEM_RELEASE)
            raise RuntimeError("aGame session cave is unexpectedly too large")

        initial = struct.pack(
            "<16I",
            int(live_cursor) & 0xFFFFFFFF,
            SAVE_TRACK_NO_SLOT,
            int(live_cursor) & 0xFFFFFFFF,
            0, 0,
            SAVE_TRACK_NO_SLOT,
            int(live_cursor) & 0xFFFFFFFF,
            0xFFFFFFFF,
            0,
            SAVE_TRACK_NO_SLOT,
            int(live_cursor) & 0xFFFFFFFF,
            0,
            int(live_cursor) & 0xFFFFFFFF,
            0,
            int(live_cursor) & 0xFFFFFFFF,
            0,
        )
        if len(checked_group3_mask) != SAVE_TRACK_CHECKED_G3_MASK_SIZE:
            kernel32.VirtualFreeEx(process, remote, 0, MEM_RELEASE)
            raise ValueError("checked Group-3 mask must be exactly 64 bytes")

        slot_patch = make_rel32_call(slot_call, slot_cave_addr)
        sync_patch = make_rel32_call(sync_call, writer_cave_addr)
        async_patch = make_rel32_call(async_call, writer_cave_addr)
        load_patch = make_rel32_jmp(
            load_entry, load_cave_addr, len(LOAD_SLOT_APPLY_ORIGINAL)
        )
        agame_patch = make_rel32_jmp(
            agame_entry, agame_cave_addr, len(AGAME_CTOR_ORIGINAL)
        )

        patched_slot = patched_sync = patched_async = patched_load = patched_agame = False
        safe_to_free = True
        try:
            self.pm.write_bytes(data_base, initial, len(initial))
            self.pm.write_bytes(
                data_base + SAVE_TRACK_CHECKED_G3_MASK_OFFSET,
                checked_group3_mask,
                len(checked_group3_mask),
            )
            self.pm.write_bytes(slot_cave_addr, slot_cave, len(slot_cave))
            self.pm.write_bytes(writer_cave_addr, writer_cave, len(writer_cave))
            self.pm.write_bytes(load_cave_addr, load_cave, len(load_cave))
            self.pm.write_bytes(
                load_trampoline_addr, load_trampoline, len(load_trampoline)
            )
            self.pm.write_bytes(agame_cave_addr, agame_cave, len(agame_cave))

            # Writer wrappers first. The aGame classifier must be armed before
            # the selected-load hook so no load can set a pending marker without
            # a corresponding constructor consumer. Save-slot capture remains last.
            # arm the save-slot capture last so no save can become pending before
            # both writer completion paths are wrapped.
            self._protect_and_write_code(sync_call, sync_patch)
            patched_sync = True
            self._protect_and_write_code(async_call, async_patch)
            patched_async = True
            self._protect_and_write_code(agame_entry, agame_patch)
            patched_agame = True
            self._protect_and_write_code(load_entry, load_patch)
            patched_load = True
            self._protect_and_write_code(slot_call, slot_patch)
            patched_slot = True
        except Exception:
            try:
                if patched_slot:
                    self._protect_and_write_code(slot_call, SAVE_SLOT_SERIALIZE_ORIGINAL)
                if patched_load:
                    self._protect_and_write_code(load_entry, LOAD_SLOT_APPLY_ORIGINAL)
                if patched_agame:
                    self._protect_and_write_code(agame_entry, AGAME_CTOR_ORIGINAL)
                if patched_async:
                    self._protect_and_write_code(async_call, SAVE_WRITE_ASYNC_ORIGINAL)
                if patched_sync:
                    self._protect_and_write_code(sync_call, SAVE_WRITE_SYNC_ORIGINAL)
            except Exception:
                safe_to_free = False
                logger.exception(
                    "RE1R: save/load tracking hook installation rollback failed; "
                    "remote code will be left allocated for safety."
                )
            if safe_to_free:
                kernel32.VirtualFreeEx(process, remote, 0, MEM_RELEASE)
            raise

        self._save_track_remote_base = remote_base
        self._save_track_slot_patch = slot_patch
        self._save_track_sync_patch = sync_patch
        self._save_track_async_patch = async_patch
        self._save_track_load_patch = load_patch
        self._save_track_agame_patch = agame_patch
        self.save_tracking_hooks_installed = True

    def write_tracking_cursors(
        self,
        save_state_cursor: int,
        global_delivery_cursor: int,
    ) -> None:
        if (
            self.pm is None
            or not self.save_tracking_hooks_installed
            or not self._save_track_remote_base
        ):
            return
        try:
            self.pm.write_bytes(
                self._save_track_remote_base + SAVE_TRACK_LIVE_CURSOR_OFFSET,
                struct.pack("<I", int(save_state_cursor) & 0xFFFFFFFF),
                4,
            )
            self.pm.write_bytes(
                self._save_track_remote_base + SAVE_TRACK_GLOBAL_CURSOR_OFFSET,
                struct.pack("<I", int(global_delivery_cursor) & 0xFFFFFFFF),
                4,
            )
        except Exception:
            # Tracking is auxiliary; a transient tracking write must not turn a
            # successful native AP item delivery into a delivery fault.
            return

    def write_checked_group3_mask(self, mask: bytes) -> None:
        """Mirror the server-authoritative checked randomized g3 mask."""
        if len(mask) != SAVE_TRACK_CHECKED_G3_MASK_SIZE:
            raise ValueError("checked Group-3 mask must be exactly 64 bytes")
        if (
            self.pm is None
            or not self.save_tracking_hooks_installed
            or not self._save_track_remote_base
        ):
            return
        try:
            self.pm.write_bytes(
                self._save_track_remote_base + SAVE_TRACK_CHECKED_G3_MASK_OFFSET,
                mask,
                len(mask),
            )
        except Exception:
            # As with cursor mirroring, keep this auxiliary write from turning
            # unrelated native item delivery into a delivery fault.
            return

    def write_save_tracking_cursor(self, cursor: int) -> None:
        # Backward-compatible helper for call sites where both cursors are
        # intentionally identical.
        self.write_tracking_cursors(cursor, cursor)

    def read_save_tracking_state(self) -> Optional[tuple[int, ...]]:
        if (
            self.pm is None
            or not self.save_tracking_hooks_installed
            or not self._save_track_remote_base
        ):
            return None
        try:
            raw = self.pm.read_bytes(
                self._save_track_remote_base,
                SAVE_TRACK_DATA_SIZE,
            )
        except Exception:
            return None
        if len(raw) != SAVE_TRACK_DATA_SIZE:
            return None
        return struct.unpack("<16I", raw)

    def restore_save_tracking_hooks(self) -> None:
        if not self.save_tracking_hooks_installed:
            return
        if self.pm is None or not self.process_alive():
            self._forget_save_tracking_hook_state()
            return

        slot_call = self.module_base + SAVE_SLOT_SERIALIZE_CALL_RVA
        sync_call = self.module_base + SAVE_WRITE_SYNC_CALL_RVA
        async_call = self.module_base + SAVE_WRITE_ASYNC_CALL_RVA
        load_entry = self.module_base + LOAD_SLOT_APPLY_RVA
        agame_entry = self.module_base + AGAME_CTOR_RVA

        checks = (
            (slot_call, self._save_track_slot_patch, SAVE_SLOT_SERIALIZE_ORIGINAL, "save-slot"),
            (sync_call, self._save_track_sync_patch, SAVE_WRITE_SYNC_ORIGINAL, "sync writer"),
            (async_call, self._save_track_async_patch, SAVE_WRITE_ASYNC_ORIGINAL, "async writer"),
            (load_entry, self._save_track_load_patch, LOAD_SLOT_APPLY_ORIGINAL, "load-slot"),
            (agame_entry, self._save_track_agame_patch, AGAME_CTOR_ORIGINAL, "aGame ctor"),
        )
        actuals = []
        for address, patch, original, label in checks:
            actual = self.pm.read_bytes(address, len(original))
            if actual not in {patch, original}:
                raise RuntimeError(
                    f"{label} save/load-tracking site no longer contains our patch "
                    "or the original bytes; refusing to overwrite it."
                )
            actuals.append(actual)

        # Stop accepting new save/session events before detaching writer wrappers.
        if actuals[0] == self._save_track_slot_patch:
            self._protect_and_write_code(slot_call, SAVE_SLOT_SERIALIZE_ORIGINAL)
        if actuals[4] == self._save_track_agame_patch:
            self._protect_and_write_code(agame_entry, AGAME_CTOR_ORIGINAL)
        if actuals[3] == self._save_track_load_patch:
            self._protect_and_write_code(load_entry, LOAD_SLOT_APPLY_ORIGINAL)
        if actuals[2] == self._save_track_async_patch:
            self._protect_and_write_code(async_call, SAVE_WRITE_ASYNC_ORIGINAL)
        if actuals[1] == self._save_track_sync_patch:
            self._protect_and_write_code(sync_call, SAVE_WRITE_SYNC_ORIGINAL)

        self._free_save_tracking_hook_block()
        self._forget_save_tracking_hook_state()

    def _free_pickup_hook_block(self) -> None:
        if (
            self.pm is None
            or kernel32 is None
            or not self._pickup_hook_remote_base
        ):
            self._pickup_hook_remote_base = 0
            return

        process = wintypes.HANDLE(self.pm.process_handle)
        if not kernel32.VirtualFreeEx(
            process,
            ctypes.c_void_p(self._pickup_hook_remote_base),
            0,
            MEM_RELEASE,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        self._pickup_hook_remote_base = 0

    def install_pickup_hooks(self, flags: set[int]) -> None:
        """
        Install the two runtime-tested AP pickup hooks for the supplied Group-3
        whitelist. No executable file on disk is modified.
        """
        if self.pm is None or kernel32 is None or not self.attached:
            raise RuntimeError("Cannot install pickup hooks while detached")

        if not flags:
            raise ValueError("Pickup-hook whitelist is empty")

        requested = frozenset(flags)

        if self.pickup_hooks_installed:
            if requested != self.pickup_hook_flags:
                whitelist_address = (
                    self._pickup_hook_remote_base + PICKUP_WHITELIST_OFFSET
                )
                bitset = build_group3_whitelist(set(requested))
                self.pm.write_bytes(
                    whitelist_address,
                    bitset,
                    len(bitset),
                )
                self.pickup_hook_flags = requested
            return

        hook_a = self.module_base + PICKUP_HOOK_A_RVA
        hook_b = self.module_base + PICKUP_HOOK_B_RVA

        actual_a = self.pm.read_bytes(
            hook_a,
            len(PICKUP_HOOK_A_ORIGINAL),
        )
        actual_b = self.pm.read_bytes(
            hook_b,
            len(PICKUP_HOOK_B_ORIGINAL),
        )

        if actual_a != PICKUP_HOOK_A_ORIGINAL:
            raise RuntimeError(
                "Pickup Hook A bytes do not match the tested bhd.exe build: "
                f"got {actual_a.hex(' ').upper()}"
            )

        if actual_b != PICKUP_HOOK_B_ORIGINAL:
            raise RuntimeError(
                "Pickup Hook B bytes do not match the tested bhd.exe build: "
                f"got {actual_b.hex(' ').upper()}"
            )

        process = wintypes.HANDLE(self.pm.process_handle)
        remote = kernel32.VirtualAllocEx(
            process,
            None,
            PICKUP_HOOK_BLOCK_SIZE,
            MEM_COMMIT | MEM_RESERVE,
            PAGE_EXECUTE_READWRITE,
        )

        if not remote:
            raise ctypes.WinError(ctypes.get_last_error())

        remote_base = ctypes.cast(remote, ctypes.c_void_p).value
        assert remote_base is not None

        whitelist_address = remote_base + PICKUP_WHITELIST_OFFSET
        cave_a_address = remote_base + PICKUP_HOOK_A_CAVE_OFFSET
        cave_b_address = remote_base + PICKUP_HOOK_B_CAVE_OFFSET

        cave_a = build_pickup_hook_a(
            cave_a_address,
            self.module_base,
            whitelist_address,
        )
        cave_b = build_pickup_hook_b(
            cave_b_address,
            self.module_base,
            whitelist_address,
        )

        if len(cave_a) >= PICKUP_HOOK_B_CAVE_OFFSET - PICKUP_HOOK_A_CAVE_OFFSET:
            kernel32.VirtualFreeEx(process, remote, 0, MEM_RELEASE)
            raise RuntimeError("Pickup Hook A cave is unexpectedly too large")

        if len(cave_b) >= PICKUP_HOOK_BLOCK_SIZE - PICKUP_HOOK_B_CAVE_OFFSET:
            kernel32.VirtualFreeEx(process, remote, 0, MEM_RELEASE)
            raise RuntimeError("Pickup Hook B cave is unexpectedly too large")

        patch_a = make_rel32_jmp(
            hook_a,
            cave_a_address,
            len(PICKUP_HOOK_A_ORIGINAL),
        )
        patch_b = make_rel32_jmp(
            hook_b,
            cave_b_address,
            len(PICKUP_HOOK_B_ORIGINAL),
        )

        patched_a = False
        patched_b = False
        safe_to_free = True

        try:
            bitset = build_group3_whitelist(set(requested))
            self.pm.write_bytes(whitelist_address, bitset, len(bitset))
            self.pm.write_bytes(cave_a_address, cave_a, len(cave_a))
            self.pm.write_bytes(cave_b_address, cave_b, len(cave_b))

            if not kernel32.FlushInstructionCache(
                process,
                ctypes.c_void_p(remote_base),
                PICKUP_HOOK_BLOCK_SIZE,
            ):
                raise ctypes.WinError(ctypes.get_last_error())

            # Hook B goes in first. This avoids a window where Hook A could
            # allow a full-inventory pickup into unmodified vanilla case 4.
            self._protect_and_write_code(hook_b, patch_b)
            patched_b = True
            self._protect_and_write_code(hook_a, patch_a)
            patched_a = True

        except Exception:
            # Roll back any partial installation before freeing code caves.
            try:
                if patched_a:
                    self._protect_and_write_code(
                        hook_a,
                        PICKUP_HOOK_A_ORIGINAL,
                    )
                if patched_b:
                    self._protect_and_write_code(
                        hook_b,
                        PICKUP_HOOK_B_ORIGINAL,
                    )
            except Exception:
                safe_to_free = False
                logger.exception(
                    "RE1R: pickup-hook installation rollback failed. "
                    "The remote code block is being left allocated so the "
                    "game cannot jump into freed memory. Close bhd.exe."
                )

            if safe_to_free:
                kernel32.VirtualFreeEx(process, remote, 0, MEM_RELEASE)
            raise

        self._pickup_hook_remote_base = remote_base
        self._pickup_hook_patch_a = patch_a
        self._pickup_hook_patch_b = patch_b
        self.pickup_hook_flags = requested
        self.pickup_hooks_installed = True

    def restore_pickup_hooks(self) -> None:
        if not self.pickup_hooks_installed:
            return

        if self.pm is None or not self.process_alive():
            self._forget_pickup_hook_state()
            return

        hook_a = self.module_base + PICKUP_HOOK_A_RVA
        hook_b = self.module_base + PICKUP_HOOK_B_RVA

        actual_a = self.pm.read_bytes(hook_a, len(PICKUP_HOOK_A_ORIGINAL))
        actual_b = self.pm.read_bytes(hook_b, len(PICKUP_HOOK_B_ORIGINAL))

        # Refuse to overwrite an unknown third-party modification.
        if actual_a not in {self._pickup_hook_patch_a, PICKUP_HOOK_A_ORIGINAL}:
            raise RuntimeError(
                "Pickup Hook A no longer contains our patch or the original "
                "bytes; refusing to overwrite it."
            )
        if actual_b not in {self._pickup_hook_patch_b, PICKUP_HOOK_B_ORIGINAL}:
            raise RuntimeError(
                "Pickup Hook B no longer contains our patch or the original "
                "bytes; refusing to overwrite it."
            )

        # Remove A first so no new capacity bypass can enter case 4 while B is
        # being restored.
        if actual_a == self._pickup_hook_patch_a:
            self._protect_and_write_code(
                hook_a,
                PICKUP_HOOK_A_ORIGINAL,
            )

        if actual_b == self._pickup_hook_patch_b:
            self._protect_and_write_code(
                hook_b,
                PICKUP_HOOK_B_ORIGINAL,
            )

        self._free_pickup_hook_block()
        self._forget_pickup_hook_state()

    def set_inventory_slot(
        self,
        context: int,
        slot: int,
        item_id: int,
        quantity: int,
    ) -> None:
        if not 0 <= slot < JILL_INVENTORY_SLOTS:
            raise ValueError(f"Invalid Jill inventory slot {slot}")

        if not 1 <= item_id <= MAX_GAME_ITEM_ID:
            raise ValueError(f"Invalid RE1R item ID 0x{item_id:X}")

        stub = self._make_thiscall4_stub(
            context,
            self.module_base + SET_INVENTORY_SLOT_RVA,
            slot,
            item_id,
            quantity,
            1,  # native item bookkeeping flag
        )
        self._remote_call(stub)

    def set_itembox_slot(
        self,
        context: int,
        slot: int,
        item_id: int,
        quantity: int,
    ) -> None:
        if not 0 <= slot < ITEMBOX_SLOTS:
            raise ValueError(f"Invalid Item Box slot {slot}")

        if not 1 <= item_id <= MAX_GAME_ITEM_ID:
            raise ValueError(f"Invalid RE1R item ID 0x{item_id:X}")

        stub = self._make_thiscall4_stub(
            context,
            self.module_base + SET_ITEMBOX_SLOT_RVA,
            ITEMBOX_BANK,
            slot,
            item_id,
            quantity,
        )
        self._remote_call(stub)

    def open_itembox(self) -> bool:
        """
        Open the native shared Item Box UI through the exact runtime-proven
        vanilla call sequence, but only in Mansion - West Wing Outer Stairway
        (stage 1, room 0x12). Returns False when safety guards are not met.
        """
        if not self.attached or not self.ordinary_gameplay_active():
            return False

        if not self.virtual_itembox_room_active():
            return False

        context = self.get_context()
        if not context:
            return False

        ui_manager = self.read_uint(
            self.module_base + UI_MANAGER_PTR_RVA
        )
        if not ui_manager:
            return False

        stub = self._make_open_itembox_stub(
            context,
            ui_manager,
            self.module_base + CLEAR_CONTEXT_FLAG_RVA,
            self.module_base + OPEN_UI_RVA,
        )
        self._remote_call(stub)
        return True


def group3_flag_is_set(bank: bytes, flag: int) -> bool:
    byte_index = flag >> 3
    bit_index = flag & 7
    return bool(bank[byte_index] & (1 << bit_index))


def checked_flags_from_bank(
    bank: bytes,
    catalog: dict[int, str],
) -> set[int]:
    return {
        flag
        for flag in catalog
        if group3_flag_is_set(bank, flag)
    }


# ---------------------------------------------------------------------------
# Item delivery planner
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeliverySpec:
    game_item_id: int
    quantity: int


@dataclass(frozen=True)
class SlotWrite:
    slot: int
    item_id: int
    quantity: int
    reason: str


@dataclass
class RecoveryPlan:
    re1r_slot: Optional[int]
    saved_cursor: int
    target_cursor: int
    next_index: int

    @property
    def complete(self) -> bool:
        return self.next_index >= self.target_cursor


def plan_delivery(
    slots: list[tuple[int, int]],
    item_id: int,
    incoming_quantity: int,
    stackable: bool,
) -> Optional[list[SlotWrite]]:
    """
    Plan a complete delivery without modifying game memory.

    None means the complete item cannot fit in this destination.
    """
    working = list(slots)
    writes: list[SlotWrite] = []
    remaining = incoming_quantity

    if stackable:
        # Fill matching stacks first.
        for slot, (existing_item, existing_qty) in enumerate(working):
            if remaining == 0:
                break

            if existing_item != item_id:
                continue

            if not 0 <= existing_qty <= MAX_STACK:
                continue

            room = MAX_STACK - existing_qty
            if room <= 0:
                continue

            added = min(room, remaining)
            new_qty = existing_qty + added

            writes.append(
                SlotWrite(
                    slot,
                    item_id,
                    new_qty,
                    f"stack +{added}",
                )
            )
            working[slot] = (item_id, new_qty)
            remaining -= added

        # Spill remainder into empty entries.
        for slot, value in enumerate(working):
            if remaining == 0:
                break

            if value != (0, 0):
                continue

            placed = min(MAX_STACK, remaining)

            writes.append(
                SlotWrite(
                    slot,
                    item_id,
                    placed,
                    f"new stack {placed}",
                )
            )
            working[slot] = (item_id, placed)
            remaining -= placed

    else:
        # Non-stackables consume one entry. Quantity is preserved exactly;
        # RE1R has item types where this field is meaningful even outside
        # conventional ammo-style stacks.
        for slot, value in enumerate(working):
            if value == (0, 0):
                writes.append(
                    SlotWrite(
                        slot,
                        item_id,
                        incoming_quantity,
                        "new slot",
                    )
                )
                remaining = 0
                break

    if remaining != 0:
        return None

    return writes


def verify_plan(
    slots: list[tuple[int, int]],
    plan: list[SlotWrite],
) -> bool:
    return all(
        slots[write.slot] == (write.item_id, write.quantity)
        for write in plan
    )


# ---------------------------------------------------------------------------
# Archipelago context
# ---------------------------------------------------------------------------

class RE1RCommandProcessor(ClientCommandProcessor):
    def _cmd_re1r(self) -> bool:
        """Show RE1R process, gameplay, catalog, and location-map status."""
        ctx: RE1RContext = self.ctx

        attached = ctx.memory.attached
        loaded = False

        if attached:
            loaded, _ = ctx.memory.gameplay_loaded()

        pending = max(
            0,
            len(ctx.items_received) - ctx.next_delivery_index,
        )

        self.output(
            "RE1R: "
            f"process={'attached' if attached else 'not attached'}, "
            f"gameplay={'loaded' if loaded else 'not loaded'}, "
            f"catalog={len(ctx.flag_to_name)}, "
            f"AP locations={len(ctx.flag_to_ap_id)}, "
            f"AP items={len(ctx.item_delivery_specs)}, "
            f"item_map={'ready' if ctx.delivery_map_ready else 'not-ready'}, "
            f"delivery={'armed' if ctx.item_delivery_armed else 'disarmed'}, "
            f"pickup_hooks={ctx.pickup_hook_status()}, "
            f"save_hooks={'active' if ctx.memory.save_tracking_hooks_installed else ('faulted' if ctx.save_tracking_hook_faulted else 'inactive')}, "
            f"next_item_index={ctx.next_delivery_index}, "
            f"recovery={ctx.recovery_status()}, "
            f"pending={pending}"
        )
        return True

    def _cmd_re1rsaves(self) -> bool:
        """Show tracked AP delivery cursors for RE1R save slots."""
        ctx: RE1RContext = self.ctx
        cursors = ctx.current_saved_slot_cursors()
        if not cursors:
            self.output(
                "RE1R: no tracked successful saves for the current AP "
                "seed/slot yet."
            )
            return True
        summary = ", ".join(
            f"slot {slot + 1}=cursor {cursor}"
            for slot, cursor in sorted(cursors.items())
        )
        self.output(f"RE1R: tracked saves: {summary}")
        return True

    def _cmd_re1rarm(self) -> bool:
        """Arm world randomization and delivery from the next AP item."""
        ctx: RE1RContext = self.ctx

        if not ctx.delivery_map_ready:
            self.output(
                "RE1R: real item delivery map is not ready. Connect to a "
                "seed generated with RE1R APWorld 0.3.2+ first."
            )
            return False

        if (
            ctx.server
            and ctx.slot is not None
            and not ctx.pickup_hooks_allowed
        ):
            self.output(
                "RE1R: cannot arm world randomization because the connected "
                "AP world did not resolve the complete location catalog."
            )
            return False

        ctx.next_delivery_index = len(ctx.items_received)
        ctx.sync_tracking_cursors()
        ctx.item_delivery_armed = True
        ctx.delivery_faulted = False
        ctx.pickup_hook_faulted = False
        ctx._last_delivery_block = None
        ctx.watcher_event.set()

        self.output(
            "RE1R: randomizer armed from AP ReceivedItems index "
            f"{ctx.next_delivery_index}. Existing received items were skipped; "
            "pickup hooks will activate when the game is in a playable room."
        )
        return True


class RE1RContext(CommonContext):
    # Keep this None during CommonContext.__init__ so the client can start
    # before a local RE1R APWorld is installed. CommonClient otherwise tries
    # to read RE1R from the local network_data_package immediately and raises
    # KeyError if the world is not registered yet.
    game = None

    # Receive all item packets. APWorld 0.3.0+ supplies the native RE1R
    # delivery tuple for every stable AP item ID through slot_data.
    items_handling = 0b111
    command_processor = RE1RCommandProcessor

    def __init__(
        self,
        server_address: Optional[str],
        password: Optional[str],
        location_file: Path,
        poll_interval: float,
        replay_received_items: bool,
        itembox_hotkey: str,
        save_tracking_file: Path,
    ) -> None:
        super().__init__(server_address, password)

        # From this point on, identify as RE1R. If/when we connect to a server
        # that has an RE1R slot, CommonClient can obtain that game's data
        # package from the server even if the APWorld is not installed locally.
        self.game = GAME_NAME

        self.location_file = location_file
        self.poll_interval = poll_interval
        self.itembox_hotkey = itembox_hotkey.upper()
        self.itembox_hotkey_vk = FUNCTION_KEY_VKS[self.itembox_hotkey]
        self._itembox_hotkey_was_down = False
        self.save_tracking_file = save_tracking_file
        self.ap_seed_name: Optional[str] = None
        self.save_tracking_hook_faulted = False
        self._last_save_request_seq = 0
        self._last_save_completed_seq = 0
        self._last_load_seq = 0
        self._last_fresh_seq = 0
        self._last_synced_checked_group3_mask: Optional[bytes] = None
        self._fresh_group3_restore_pending = False
        self.recovery_plan: Optional[RecoveryPlan] = None
        self._last_recovery_block: Optional[tuple[int, str]] = None

        self.flag_to_name = load_location_catalog(location_file)
        self.flag_to_ap_id: dict[int, int] = {}

        self.memory = RE1RMemory()

        # Persistent during this client process. CommonClient uses
        # locations_checked to resend checks after an AP reconnect.
        self.locally_observed_flags: set[int] = set()

        self._last_received_log_count = 0

        # Real AP item delivery is seed-authoritative. APWorld 0.3.0+ sends a
        # compact AP-item-ID -> native RE1R (item ID, quantity) map in slot_data.
        # This keeps the client independent from hard-coded AP item IDs/names.
        self.item_delivery_specs: dict[int, DeliverySpec] = {}
        self.delivery_map_ready = False
        self.item_id_scheme_version: Optional[int] = None
        self.connected_slot_data: dict = {}

        self.next_delivery_index = 0
        self.item_delivery_armed = False
        self.replay_received_items = replay_received_items
        self._replay_autoarm_pending = replay_received_items

        # A mid-delivery native-call failure can make the exact game state
        # uncertain. Disarm instead of automatically retrying and potentially
        # duplicating a partial delivery.
        self.delivery_faulted = False
        self._last_delivery_block: Optional[tuple[int, str]] = None

        # World-side randomization is enabled only after a successful AP
        # Connected packet resolves the complete local location catalog. Hooks
        # are then installed only while item delivery is armed.
        self.pickup_hooks_allowed = False
        self.pickup_hook_faulted = False

        # Runtime victory state. The game-side condition is an active
        # aEnding engine state. This is intentionally client-process-local for
        # now; durable save/restart persistence remains a separate milestone.
        self.runtime_goal_reached = False

    def _save_profile_identity(self) -> Optional[tuple[str, int, int, str]]:
        seed_name = self.ap_seed_name or getattr(self, "seed_name", None)
        slot = getattr(self, "slot", None)
        team = getattr(self, "team", None)
        if not seed_name or slot is None or team is None:
            return None
        return str(seed_name), int(team), int(slot), str(self.auth or "")

    def _save_profile_key(self) -> Optional[str]:
        identity = self._save_profile_identity()
        if identity is None:
            return None
        seed_name, team, slot, _slot_name = identity
        raw = f"{seed_name}\0{team}\0{slot}".encode(
            "utf-8", errors="replace"
        )
        return hashlib.sha256(raw).hexdigest()[:24]

    def _load_save_tracking_document(self) -> dict:
        path = self.save_tracking_file
        if not path.exists():
            return {"schema": 1, "profiles": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception(
                "RE1R: could not read save-tracking sidecar %s.", path
            )
            return {"schema": 1, "profiles": {}}
        if not isinstance(data, dict) or data.get("schema") != 1:
            logger.warning(
                "RE1R: ignoring unsupported save-tracking sidecar format in %s.",
                path,
            )
            return {"schema": 1, "profiles": {}}
        if not isinstance(data.get("profiles"), dict):
            data["profiles"] = {}
        return data

    def _write_save_tracking_document(self, data: dict) -> None:
        path = self.save_tracking_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)

    def record_successful_game_save(self, re1r_slot: int, cursor: int) -> None:
        identity = self._save_profile_identity()
        profile_key = self._save_profile_key()
        if identity is None or profile_key is None:
            logger.warning(
                "RE1R: successful game save detected for slot %d at AP cursor "
                "%d, but AP seed/slot identity is not available; sidecar was "
                "not updated.",
                re1r_slot + 1, cursor,
            )
            return

        seed_name, team, ap_slot, ap_slot_name = identity
        data = self._load_save_tracking_document()
        profiles = data.setdefault("profiles", {})
        profile = profiles.setdefault(profile_key, {})
        profile.update({
            "seed_name": seed_name,
            "ap_team": team,
            "ap_slot": ap_slot,
            "ap_slot_name": ap_slot_name,
        })
        slots = profile.setdefault("re1r_slots", {})
        slots[str(re1r_slot)] = {
            "display_slot": re1r_slot + 1,
            "saved_delivery_cursor": int(cursor),
            "last_saved_item_index": int(cursor) - 1,
            "saved_at_unix": time.time(),
        }
        self._write_save_tracking_document(data)
        logger.info(
            "RE1R: successful save to game slot %d recorded at AP delivery "
            "cursor %d (%s).",
            re1r_slot + 1, cursor, self.save_tracking_file,
        )

    def process_save_tracking_events(self) -> None:
        state = self.memory.read_save_tracking_state()
        if state is None:
            return

        (
            live_cursor, captured_slot, captured_cursor, pending,
            request_seq, completed_slot, completed_cursor, completed_result,
            completed_seq, loaded_slot, loaded_cursor, load_seq, global_cursor,
            agame_pending_load, fresh_cursor, fresh_seq,
        ) = state

        if request_seq != self._last_save_request_seq:
            delta = (request_seq - self._last_save_request_seq) & 0xFFFFFFFF
            if delta > 1:
                logger.warning(
                    "RE1R: %d save requests occurred between watcher polls; "
                    "only the latest captured slot/cursor is available.", delta
                )
            self._last_save_request_seq = request_seq
            if 0 <= captured_slot < 8:
                logger.info(
                    "RE1R: gameplay save committed to slot %d; captured AP "
                    "delivery cursor %d%s.",
                    captured_slot + 1, captured_cursor,
                    " (write pending)" if pending else "",
                )

        if completed_seq != self._last_save_completed_seq:
            delta = (completed_seq - self._last_save_completed_seq) & 0xFFFFFFFF
            if delta > 1:
                logger.warning(
                    "RE1R: %d tracked save completions occurred between "
                    "watcher polls; only the latest result is available.", delta
                )
            self._last_save_completed_seq = completed_seq

            if not 0 <= completed_slot < 8:
                logger.warning(
                    "RE1R: tracked save completed with invalid slot 0x%08X.",
                    completed_slot,
                )
                return

            if completed_result == 0:
                try:
                    self.record_successful_game_save(
                        completed_slot, completed_cursor
                    )
                except Exception:
                    logger.exception(
                        "RE1R: successful save was detected, but writing the "
                        "AP save-tracking sidecar failed."
                    )
            else:
                logger.warning(
                    "RE1R: game save to slot %d failed with storage result %d; "
                    "AP save cursor was NOT advanced.",
                    completed_slot + 1, completed_result,
                )

        if load_seq != self._last_load_seq:
            delta = (load_seq - self._last_load_seq) & 0xFFFFFFFF
            if delta > 1:
                logger.warning(
                    "RE1R: %d selected save-slot loads occurred between watcher "
                    "polls; only the latest slot is available.", delta
                )
            self._last_load_seq = load_seq

            if 0 <= loaded_slot < 8:
                restored_flags = self.current_checked_group3_flags()
                if restored_flags:
                    # The game-side load wrapper already ORed this same mask
                    # into the freshly applied save before FUN_0042F2F0
                    # returned. Keep local observation state aligned so the
                    # first room scan does not treat restoration as new finds.
                    self.locally_observed_flags |= restored_flags
                    logger.info(
                        "RE1R: load slot %d reapplied %d AP-checked randomized "
                        "Group-3 location flags before gameplay resumed.",
                        loaded_slot + 1, len(restored_flags),
                    )
                self.arm_recovery_for_loaded_slot(loaded_slot, loaded_cursor)
            else:
                logger.warning(
                    "RE1R: load tracker published invalid slot 0x%08X.",
                    loaded_slot,
                )

        if fresh_seq != self._last_fresh_seq:
            delta = (fresh_seq - self._last_fresh_seq) & 0xFFFFFFFF
            if delta > 1:
                logger.warning(
                    "RE1R: %d Fresh New Game starts occurred between watcher "
                    "polls; only the latest captured cursor is available.", delta
                )
            self._last_fresh_seq = fresh_seq

            restored_flags = self.current_checked_group3_flags()
            self.locally_observed_flags |= restored_flags
            self._fresh_group3_restore_pending = bool(restored_flags)
            logger.info(
                "RE1R: Fresh New Game detected with no preceding save-slot "
                "load; AP recovery baseline is cursor 0 (captured global "
                "cursor %d).",
                fresh_cursor,
            )
            self.arm_recovery_for_fresh_new_game(fresh_cursor)

    def current_saved_slot_cursors(self) -> dict[int, int]:
        profile_key = self._save_profile_key()
        if profile_key is None:
            return {}
        data = self._load_save_tracking_document()
        profile = data.get("profiles", {}).get(profile_key, {})
        raw_slots = profile.get("re1r_slots", {})
        result: dict[int, int] = {}
        if isinstance(raw_slots, dict):
            for raw_slot, entry in raw_slots.items():
                try:
                    slot = int(raw_slot)
                    cursor = int(entry["saved_delivery_cursor"])
                except Exception:
                    continue
                if 0 <= slot < 8 and cursor >= 0:
                    result[slot] = cursor
        return result

    def current_checked_group3_flags(self) -> set[int]:
        """Map AP's authoritative checked locations back to our g3 whitelist."""
        if (
            not self.flag_to_ap_id
            or len(self.flag_to_ap_id) != len(self.flag_to_name)
        ):
            return set()
        checked_ids = set(self.locations_checked)
        return {
            flag
            for flag, location_id in self.flag_to_ap_id.items()
            if location_id in checked_ids
        }

    def current_checked_group3_mask(self) -> bytes:
        return build_group3_whitelist(self.current_checked_group3_flags())

    def sync_checked_group3_mask(self, force: bool = False) -> None:
        mask = self.current_checked_group3_mask()
        if not force and mask == self._last_synced_checked_group3_mask:
            return
        self.memory.write_checked_group3_mask(mask)
        self._last_synced_checked_group3_mask = mask

    def apply_pending_fresh_group3_restore(
        self,
        state_root: Optional[int] = None,
        final: bool = False,
    ) -> None:
        """
        Reapply AP-authoritative randomized g3 flags during Fresh New Game.

        The aGame constructor is a semantic session-start event, but it is too
        early to assume the new-game persistent banks have finished their own
        initialization. While the Fresh New Game is coming up, repeatedly OR
        the AP mask into the current StateRoot; do one final application when a
        playable RoomRecord set exists, then retire the pending restore.
        """
        if not self._fresh_group3_restore_pending:
            return
        if state_root is None:
            state_root = self.memory.get_state_root()
        if state_root is None:
            return

        mask = self.current_checked_group3_mask()
        if not any(mask):
            self._fresh_group3_restore_pending = False
            return

        if not self.memory.or_group3_mask(state_root, mask):
            return

        if final:
            count = len(self.current_checked_group3_flags())
            self._fresh_group3_restore_pending = False
            logger.info(
                "RE1R: Fresh New Game reapplied %d AP-checked randomized "
                "Group-3 location flags before normal scanning resumed.",
                count,
            )

    def arm_recovery_for_fresh_new_game(self, captured_global_cursor: int) -> None:
        """Treat Fresh New Game as an implicit RE1R save state at AP cursor 0."""
        self.recovery_plan = None
        self._last_recovery_block = None

        target_cursor = int(captured_global_cursor)
        if target_cursor > self.next_delivery_index:
            logger.warning(
                "RE1R: Fresh New Game captured AP cursor %d ahead of client "
                "delivery cursor %d; clamping recovery target.",
                target_cursor, self.next_delivery_index,
            )
            target_cursor = self.next_delivery_index
        if target_cursor > len(self.items_received):
            logger.warning(
                "RE1R: Fresh New Game needs ReceivedItems through cursor %d, "
                "but only %d entries are currently available; clamping.",
                target_cursor, len(self.items_received),
            )
            target_cursor = len(self.items_received)

        if target_cursor <= 0:
            self.sync_tracking_cursors()
            logger.info(
                "RE1R: Fresh New Game has no previously delivered AP items "
                "to recover."
            )
            return

        self.recovery_plan = RecoveryPlan(
            re1r_slot=None,
            saved_cursor=0,
            target_cursor=target_cursor,
            next_index=0,
        )
        self.sync_tracking_cursors()
        logger.warning(
            "RE1R: Fresh New Game started after %d AP items had already been "
            "delivered; queued recovery of indices 0..%d into the native Item Box.",
            target_cursor, target_cursor - 1,
        )

    def effective_save_state_cursor(self) -> int:
        """AP delivery prefix currently represented by the loaded RE1R state."""
        if self.recovery_plan is not None and not self.recovery_plan.complete:
            return self.recovery_plan.next_index
        return self.next_delivery_index

    def sync_tracking_cursors(self) -> None:
        self.memory.write_tracking_cursors(
            self.effective_save_state_cursor(),
            self.next_delivery_index,
        )

    def arm_recovery_for_loaded_slot(
        self,
        re1r_slot: int,
        captured_global_cursor: int,
    ) -> None:
        # Every real slot load supersedes any older pending recovery plan.
        self.recovery_plan = None
        self._last_recovery_block = None

        cursors = self.current_saved_slot_cursors()
        if re1r_slot not in cursors:
            logger.warning(
                "RE1R: loaded game slot %d, but no successful AP save cursor is "
                "tracked for this AP seed/slot; automatic item recovery is "
                "skipped for this load.",
                re1r_slot + 1,
            )
            self.sync_tracking_cursors()
            return

        saved_cursor = int(cursors[re1r_slot])
        target_cursor = int(captured_global_cursor)

        if target_cursor > self.next_delivery_index:
            # The hook snapshots the separately mirrored global cursor. It
            # should normally equal next_delivery_index; clamp defensively if a
            # transient watcher write was observed out of order.
            logger.warning(
                "RE1R: load slot %d captured AP cursor %d ahead of the client "
                "delivery cursor %d; clamping recovery target.",
                re1r_slot + 1, target_cursor, self.next_delivery_index,
            )
            target_cursor = self.next_delivery_index

        if target_cursor > len(self.items_received):
            logger.warning(
                "RE1R: load slot %d needs ReceivedItems through cursor %d, but "
                "only %d entries are currently available; clamping recovery "
                "target until reconnect/restart resilience is implemented.",
                re1r_slot + 1, target_cursor, len(self.items_received),
            )
            target_cursor = len(self.items_received)

        if saved_cursor > target_cursor:
            logger.warning(
                "RE1R: tracked save cursor %d for slot %d is ahead of the "
                "current delivered cursor %d; no recovery will be attempted.",
                saved_cursor, re1r_slot + 1, target_cursor,
            )
            self.sync_tracking_cursors()
            return

        if saved_cursor == target_cursor:
            logger.info(
                "RE1R: loaded game slot %d at AP cursor %d; no post-save AP "
                "items need recovery.",
                re1r_slot + 1, saved_cursor,
            )
            self.sync_tracking_cursors()
            return

        self.recovery_plan = RecoveryPlan(
            re1r_slot=re1r_slot,
            saved_cursor=saved_cursor,
            target_cursor=target_cursor,
            next_index=saved_cursor,
        )
        self.sync_tracking_cursors()
        logger.warning(
            "RE1R: loaded game slot %d saved at AP cursor %d while %d AP items "
            "had already been delivered; queued recovery of indices %d..%d "
            "into the native Item Box.",
            re1r_slot + 1, saved_cursor, target_cursor,
            saved_cursor, target_cursor - 1,
        )

    def process_pending_recovery_items(self) -> bool:
        """Replay missing post-save items. Return True once fully recovered."""
        plan = self.recovery_plan
        if plan is None or plan.complete:
            if plan is not None and plan.complete:
                self.recovery_plan = None
                self._last_recovery_block = None
                self.sync_tracking_cursors()
            return True

        if (
            not self.item_delivery_armed
            or self.delivery_faulted
            or not self.delivery_map_ready
        ):
            return False

        while plan.next_index < plan.target_cursor:
            index = plan.next_index
            try:
                result = self.deliver_received_item(index)
            except Exception:
                self.delivery_faulted = True
                self.item_delivery_armed = False
                logger.exception(
                    "RE1R: recovery delivery failed at AP item index %d. "
                    "Automatic delivery has been DISARMED to avoid duplicating "
                    "a potentially partial write.",
                    index,
                )
                return False

            if result == "delivered":
                plan.next_index += 1
                self.sync_tracking_cursors()
                self._last_recovery_block = None
                source = (
                    "Fresh New Game"
                    if plan.re1r_slot is None
                    else f"loaded game slot {plan.re1r_slot + 1}"
                )
                logger.info(
                    "RE1R: recovered AP item index %d for %s (%d/%d).",
                    index, source,
                    plan.next_index - plan.saved_cursor,
                    plan.target_cursor - plan.saved_cursor,
                )
                continue

            block = (index, result)
            if block != self._last_recovery_block:
                if result == "unmapped":
                    logger.warning(
                        "RE1R: recovery paused at AP item index %d because the "
                        "connected seed has no native delivery mapping for it.",
                        index,
                    )
                else:
                    logger.info(
                        "RE1R: recovery paused at AP item index %d because the "
                        "complete delivery does not currently fit in the Item Box.",
                        index,
                    )
                self._last_recovery_block = block
            return False

        if plan.re1r_slot is None:
            logger.info(
                "RE1R: Fresh New Game AP item recovery is complete at cursor %d. "
                "Normal ReceivedItems delivery may resume.",
                plan.target_cursor,
            )
        else:
            logger.info(
                "RE1R: post-save AP item recovery for game slot %d is complete "
                "at cursor %d. Normal ReceivedItems delivery may resume.",
                plan.re1r_slot + 1, plan.target_cursor,
            )
        self.recovery_plan = None
        self._last_recovery_block = None
        self.sync_tracking_cursors()
        return True

    def recovery_status(self) -> str:
        plan = self.recovery_plan
        if plan is None:
            return "idle"
        source = (
            "newgame"
            if plan.re1r_slot is None
            else f"slot{plan.re1r_slot + 1}"
        )
        return f"{source}:{plan.next_index}->{plan.target_cursor}"

    def item_box_hotkey_pressed(self) -> bool:
        """
        Edge-triggered global function-key poll. Holding the key produces only
        one open request; another request requires a release and new press.
        """
        if user32 is None:
            return False

        down = bool(user32.GetAsyncKeyState(self.itembox_hotkey_vk) & 0x8000)
        pressed = down and not self._itembox_hotkey_was_down
        self._itembox_hotkey_was_down = down
        return pressed

    async def sync_runtime_goal(self) -> None:
        """Report CLIENT_GOAL once the confirmed aEnding state is observed."""
        if not self.runtime_goal_reached or self.finished_game:
            return
        if not self.server or self.slot is None:
            return

        await self.send_msgs([
            {"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}
        ])
        self.finished_game = True
        logger.info(
            "RE1R: active aEnding confirmed; Archipelago CLIENT_GOAL sent."
        )

    def pickup_hook_status(self) -> str:
        if self.memory.pickup_hooks_installed:
            return "active"
        if self.pickup_hook_faulted:
            return "faulted"
        if not self.pickup_hooks_allowed:
            return "not-ready"
        if self.item_delivery_armed:
            return "pending"
        return "disarmed"

    def reconcile_pickup_hooks(self) -> None:
        should_be_active = (
            self.item_delivery_armed
            and not self.delivery_faulted
            and not self.pickup_hook_faulted
            and self.pickup_hooks_allowed
            and self.delivery_map_ready
            and bool(self.flag_to_ap_id)
            and len(self.flag_to_ap_id) == len(self.flag_to_name)
        )

        if should_be_active:
            if not self.memory.pickup_hooks_installed:
                try:
                    self.memory.install_pickup_hooks(
                        set(self.flag_to_ap_id)
                    )
                except Exception:
                    self.pickup_hook_faulted = True
                    self.item_delivery_armed = False
                    logger.exception(
                        "RE1R: failed to install AP pickup hooks. Randomizer "
                        "delivery has been DISARMED; vanilla pickup behavior "
                        "is being left/restored wherever possible."
                    )
                    try:
                        self.memory.restore_pickup_hooks()
                    except Exception:
                        logger.exception(
                            "RE1R: additionally failed to restore a partial "
                            "pickup-hook installation. Close bhd.exe before "
                            "continuing."
                        )
                    return

                logger.info(
                    "RE1R: AP pickup hooks active for %d Group-3 locations. "
                    "Vanilla rewards are suppressed and full-inventory checks "
                    "are allowed.",
                    len(self.memory.pickup_hook_flags),
                )
            return

        if self.memory.pickup_hooks_installed:
            try:
                self.memory.restore_pickup_hooks()
            except Exception:
                self.pickup_hook_faulted = True
                logger.exception(
                    "RE1R: failed to restore AP pickup hooks. Close bhd.exe "
                    "before continuing."
                )
            else:
                logger.info("RE1R: AP pickup hooks restored/disabled.")

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super().server_auth(password_requested)

        await self.get_username()
        await self.send_connect()

    def rebuild_location_id_map(self) -> None:
        """
        Resolve our stable location names against the AP world's data package.

        This means the client does not need hard-coded Archipelago location IDs.
        The future AP world only needs to use the exact same location names.
        """
        lookup = self.location_names[self.game]

        ap_name_to_id = {
            name: location_id
            for location_id, name in lookup.items()
            if isinstance(location_id, int) and location_id >= 0
        }

        resolved: dict[int, int] = {}
        missing: list[tuple[int, str]] = []

        for flag, name in self.flag_to_name.items():
            location_id = ap_name_to_id.get(name)

            if location_id is None:
                missing.append((flag, name))
            else:
                resolved[flag] = location_id

        self.flag_to_ap_id = resolved

        if missing:
            logger.warning(
                "RE1R: resolved %d/%d catalog locations against the AP "
                "data package. %d names are missing.",
                len(resolved),
                len(self.flag_to_name),
                len(missing),
            )

            for flag, name in missing[:20]:
                logger.warning(
                    "RE1R: missing AP location for g3:%d: %s",
                    flag,
                    name,
                )

            if len(missing) > 20:
                logger.warning(
                    "RE1R: ... and %d additional missing location names.",
                    len(missing) - 20,
                )
        else:
            logger.info(
                "RE1R: resolved all %d Group 3 locations against the AP "
                "data package.",
                len(resolved),
            )

    async def sync_observed_checks(self) -> None:
        """
        Convert all Group 3 flags observed by this client into AP IDs and
        submit whichever of them the server still reports as missing.
        """
        if not self.flag_to_ap_id:
            return

        ids = {
            self.flag_to_ap_id[flag]
            for flag in self.locally_observed_flags
            if flag in self.flag_to_ap_id
        }

        # Keep CommonClient's reconnect-resend state populated.
        self.locations_checked |= ids

        if self.server and self.slot is not None:
            new_checks = await self.check_locations(ids)

            for location_id in sorted(new_checks):
                try:
                    name = self.location_names.lookup_in_game(
                        location_id,
                        self.game,
                    )
                except Exception:
                    name = str(location_id)

                logger.info("RE1R: location checked: %s", name)

    def rebuild_item_delivery_map(self, slot_data: dict) -> None:
        """Validate seed slot_data and build AP item ID -> native delivery."""
        self.item_delivery_specs = {}
        self.delivery_map_ready = False
        self.item_id_scheme_version = None

        if not isinstance(slot_data, dict):
            logger.error(
                "RE1R: connected slot did not provide usable slot_data."
            )
            return

        scheme = slot_data.get("item_id_scheme_version")
        raw_map = slot_data.get("ap_item_id_to_re1r_delivery")
        declared_count = slot_data.get("item_delivery_count")

        if scheme != 1:
            logger.error(
                "RE1R: unsupported/missing item_id_scheme_version %r. "
                "RE1RClient_v6 expects scheme version 1.",
                scheme,
            )
            return

        if not isinstance(raw_map, dict) or not raw_map:
            logger.error(
                "RE1R: connected seed has no real RE1R item-delivery map. "
                "Generate a fresh seed with RE1R APWorld 0.3.2+."
            )
            return

        parsed: dict[int, DeliverySpec] = {}

        try:
            for ap_item_id_text, raw_spec in raw_map.items():
                ap_item_id = int(ap_item_id_text)

                if not isinstance(raw_spec, dict):
                    raise ValueError(
                        f"item {ap_item_id}: delivery spec is not an object"
                    )

                game_item_id = int(raw_spec["game_item_id"])
                quantity = int(raw_spec["quantity"])

                if ap_item_id <= 0:
                    raise ValueError(
                        f"invalid AP item ID {ap_item_id}"
                    )
                if not 1 <= game_item_id <= MAX_GAME_ITEM_ID:
                    raise ValueError(
                        f"AP item {ap_item_id}: invalid RE1R item ID "
                        f"0x{game_item_id:X}"
                    )
                if not 0 <= quantity <= 0xFFFF:
                    raise ValueError(
                        f"AP item {ap_item_id}: invalid quantity {quantity}"
                    )

                parsed[ap_item_id] = DeliverySpec(
                    game_item_id=game_item_id,
                    quantity=quantity,
                )

        except (KeyError, TypeError, ValueError) as exc:
            logger.error(
                "RE1R: invalid item-delivery metadata in slot_data: %s",
                exc,
            )
            return

        if (
            isinstance(declared_count, int)
            and declared_count != len(parsed)
        ):
            logger.error(
                "RE1R: slot_data declares %d item mappings but contains %d.",
                declared_count,
                len(parsed),
            )
            return

        # Cross-check every mapped AP item ID against the connected data
        # package. This catches accidental APWorld/client identity mismatches.
        unknown_ids: list[int] = []
        for ap_item_id in sorted(parsed):
            try:
                name = self.item_names.lookup_in_game(
                    ap_item_id,
                    self.game,
                )
            except Exception:
                unknown_ids.append(ap_item_id)
                continue

            if name.startswith("Unknown item"):
                unknown_ids.append(ap_item_id)

        if unknown_ids:
            logger.error(
                "RE1R: %d slot_data item IDs are missing from the connected "
                "AP data package; item delivery will remain disabled.",
                len(unknown_ids),
            )
            return

        self.item_delivery_specs = parsed
        self.item_id_scheme_version = scheme
        self.delivery_map_ready = True

        logger.info(
            "RE1R: validated %d real AP item delivery mappings "
            "(item ID scheme v%d).",
            len(parsed),
            scheme,
        )

    def refresh_world_readiness(self) -> None:
        """Re-evaluate location and item mappings after connect/data-package."""
        self.rebuild_location_id_map()

        if self.connected_slot_data:
            self.rebuild_item_delivery_map(self.connected_slot_data)

        complete_locations = (
            len(self.flag_to_ap_id) == len(self.flag_to_name)
            and bool(self.flag_to_name)
        )

        self.pickup_hooks_allowed = (
            complete_locations
            and self.delivery_map_ready
        )

        if complete_locations:
            logger.info(
                "RE1R: world-pickup whitelist validated for all %d "
                "randomizable locations.",
                len(self.flag_to_name),
            )
        else:
            logger.error(
                "RE1R: only %d/%d location names resolved against the "
                "connected AP world.",
                len(self.flag_to_ap_id),
                len(self.flag_to_name),
            )

        if not self.delivery_map_ready:
            logger.error(
                "RE1R: pickup hooks will NOT activate until the real AP item "
                "delivery map is valid."
            )

        if (
            self._replay_autoarm_pending
            and self.pickup_hooks_allowed
            and self.delivery_map_ready
        ):
            self.next_delivery_index = 0
            self.item_delivery_armed = True
            self._replay_autoarm_pending = False
            logger.warning(
                "RE1R: replay mode auto-armed from ReceivedItems index 0."
            )

    def get_delivery_spec(
        self,
        ap_item_id: int,
    ) -> Optional[DeliverySpec]:
        return self.item_delivery_specs.get(ap_item_id)

    def _deliver_plan(
        self,
        context: int,
        destination: str,
        plan: list[SlotWrite],
    ) -> None:
        if destination != "itembox":
            raise ValueError(
                "RE1R AP delivery policy forbids carried-inventory delivery."
            )

        for write in plan:
            self.memory.set_itembox_slot(
                context,
                write.slot,
                write.item_id,
                write.quantity,
            )

        after = self.memory.read_itembox(context)

        if after is None or not verify_plan(after, plan):
            raise RuntimeError(
                "RE1R Item Box delivery read-back verification failed."
            )

    def deliver_received_item(
        self,
        ap_index: int,
    ) -> str:
        """
        Try to deliver one AP ReceivedItems entry.

        Returns:
            "delivered"  - complete item was written and verified
            "pending"    - no destination can fit it yet
            "unmapped"   - connected seed has no native delivery mapping
        """
        if ap_index >= len(self.items_received):
            return "pending"

        item = self.items_received[ap_index]

        try:
            item_name = self.item_names.lookup_in_game(
                item.item,
                self.game,
            )
        except Exception:
            item_name = f"item {item.item}"

        spec = self.get_delivery_spec(item.item)

        if spec is None:
            return "unmapped"

        loaded, _ = self.memory.gameplay_loaded()

        if not loaded:
            return "pending"

        context = self.memory.get_context()

        if context is None:
            return "pending"

        stackable = self.memory.item_is_stackable(
            spec.game_item_id
        )

        if stackable is None:
            return "pending"

        # AP delivery policy is intentionally Item Box-only. Never place an
        # AP ReceivedItems reward into Jill's carried inventory; this preserves
        # carried slots for forced/non-randomized vanilla interactions.
        itembox = self.memory.read_itembox(context)

        if itembox is None:
            return "pending"

        box_plan = plan_delivery(
            itembox,
            spec.game_item_id,
            spec.quantity,
            stackable,
        )

        if box_plan is None:
            return "pending"

        destination = "itembox"
        plan = box_plan

        logger.info(
            "RE1R: delivering AP item index %d: %s -> "
            "game item 0x%02X x%d via %s.",
            ap_index,
            item_name,
            spec.game_item_id,
            spec.quantity,
            destination,
        )

        self._deliver_plan(
            context,
            destination,
            plan,
        )

        logger.info(
            "RE1R: AP item index %d delivered and verified.",
            ap_index,
        )

        return "delivered"

    def process_pending_received_items(self) -> None:
        if (
            not self.item_delivery_armed
            or self.delivery_faulted
            or not self.delivery_map_ready
        ):
            return

        while self.next_delivery_index < len(self.items_received):
            index = self.next_delivery_index

            try:
                result = self.deliver_received_item(index)
            except Exception:
                self.delivery_faulted = True
                self.item_delivery_armed = False

                logger.exception(
                    "RE1R: native item delivery failed at AP item index %d. "
                    "Automatic delivery has been DISARMED to avoid duplicating "
                    "a potentially partial write. Inspect game state before "
                    "arming again.",
                    index,
                )
                return

            if result == "delivered":
                self.next_delivery_index += 1
                self.sync_tracking_cursors()
                self._last_delivery_block = None
                continue

            block = (index, result)

            if block != self._last_delivery_block:
                try:
                    item = self.items_received[index]
                    item_name = self.item_names.lookup_in_game(
                        item.item,
                        self.game,
                    )
                except Exception:
                    item_name = f"AP item index {index}"

                if result == "unmapped":
                    logger.warning(
                        "RE1R: %s has no game delivery mapping; "
                        "ReceivedItems order is paused at index %d.",
                        item_name,
                        index,
                    )
                else:
                    logger.info(
                        "RE1R: %s is pending at index %d: the complete "
                        "delivery does not currently fit in the 256-slot Item Box.",
                        item_name,
                        index,
                    )

                self._last_delivery_block = block

            # Preserve ReceivedItems order. A full Item Box or an unmapped item
            # blocks later entries until this one is resolved.
            return

    def on_package(self, cmd: str, args: dict) -> None:
        if cmd == "RoomInfo":
            seed_name = args.get("seed_name")
            if seed_name:
                self.ap_seed_name = str(seed_name)

        elif cmd == "Connected":
            logger.info(
                "RE1R: connected to Archipelago slot %s.",
                self.auth,
            )

            raw_slot_data = args.get("slot_data", {})
            self.connected_slot_data = (
                raw_slot_data
                if isinstance(raw_slot_data, dict)
                else {}
            )
            self.refresh_world_readiness()

            # The memory watcher performs actual check sync/hook reconcile on
            # its next iteration. Wake it immediately.
            self.watcher_event.set()

        elif cmd == "DataPackage":
            # A custom APWorld data package can arrive after Connected.
            # Re-evaluate both location and item identity once it is available.
            self.refresh_world_readiness()
            self.watcher_event.set()

        elif cmd == "RoomUpdate":
            self.watcher_event.set()

        elif cmd == "ReceivedItems":
            # CommonClient has already appended the packet to items_received
            # before on_package() is called.
            new_total = len(self.items_received)

            if new_total > self._last_received_log_count:
                for item in self.items_received[self._last_received_log_count:]:
                    try:
                        item_name = self.item_names.lookup_in_game(
                            item.item,
                            self.game,
                        )
                    except Exception:
                        item_name = f"item {item.item}"

                    logger.info(
                        "RE1R: received %s from player %s.",
                        item_name,
                        item.player,
                    )

                self._last_received_log_count = new_total

            # Delivery is performed by the memory watcher, never directly from
            # the network callback.
            self.watcher_event.set()


# ---------------------------------------------------------------------------
# Memory watcher
# ---------------------------------------------------------------------------

async def game_watcher(ctx: RE1RContext) -> None:
    previous_bank: Optional[bytes] = None
    was_attached = False
    was_loaded = False

    while not ctx.exit_event.is_set():
        try:
            # ---------------------------------------------------------------
            # Attach / reattach to bhd.exe
            # ---------------------------------------------------------------
            if not ctx.memory.attached:
                if ctx.memory.attach():
                    if not was_attached:
                        logger.info(
                            "RE1R: attached to %s at 0x%08X.",
                            PROCESS_NAME,
                            ctx.memory.module_base,
                        )
                    was_attached = True
                else:
                    if was_attached:
                        logger.info("RE1R: game process closed.")
                    was_attached = False
                    was_loaded = False
                    previous_bank = None
                    ctx.save_tracking_hook_faulted = False
                    ctx.recovery_plan = None
                    ctx._last_recovery_block = None
                    ctx._fresh_group3_restore_pending = False
                    ctx._last_synced_checked_group3_mask = None
                    try:
                        await asyncio.wait_for(
                            ctx.watcher_event.wait(),
                            timeout=1.0,
                        )
                        ctx.watcher_event.clear()
                    except asyncio.TimeoutError:
                        pass
                    continue

            # ---------------------------------------------------------------
            # Runtime-confirmed gameplay-save tracking.
            # ---------------------------------------------------------------
            # Install independently of AP pickup hooks/delivery arming. The
            # hook only records save metadata and preserves vanilla calls.
            if (
                not ctx.memory.save_tracking_hooks_installed
                and not ctx.save_tracking_hook_faulted
            ):
                try:
                    ctx.memory.install_save_tracking_hooks(
                        ctx.next_delivery_index,
                        ctx.current_checked_group3_mask(),
                    )
                    ctx._last_synced_checked_group3_mask = None
                    ctx.sync_checked_group3_mask(force=True)
                    ctx._last_save_request_seq = 0
                    ctx._last_save_completed_seq = 0
                    ctx._last_load_seq = 0
                    ctx._last_fresh_seq = 0
                    logger.info(
                        "RE1R: gameplay save/load/Fresh-New-Game tracking + "
                        "checked-location restore hooks active."
                    )
                except Exception:
                    ctx.save_tracking_hook_faulted = True
                    logger.exception(
                        "RE1R: failed to install gameplay-save tracking hooks. "
                        "AP save cursors and load/New Game recovery will be unavailable this run."
                    )

            if ctx.memory.save_tracking_hooks_installed:
                # Keep the game-side capture cursor synchronized before any AP
                # delivery work in this watcher iteration.
                try:
                    # Keep the in-process load wrapper supplied with AP's
                    # current authoritative checked-location mask before any
                    # selected slot can be applied.
                    ctx.sync_checked_group3_mask()
                    # Before processing a new load event, publish the global
                    # delivery cursor that existed in the pre-load game state.
                    ctx.sync_tracking_cursors()
                    ctx.process_save_tracking_events()
                    # Fresh New Game initialization may continue after aGame's
                    # constructor. Reapply checked randomized flags as soon as
                    # StateRoot is usable and keep doing so until a playable room
                    # is confirmed below.
                    ctx.apply_pending_fresh_group3_restore()
                    # A load/New Game event may have armed recovery and therefore
                    # moved the save-state cursor backward. Publish that
                    # immediately for any subsequent save.
                    ctx.sync_tracking_cursors()
                except Exception:
                    logger.exception(
                        "RE1R: save/load-tracking poll failed."
                    )

            # ---------------------------------------------------------------
            # Runtime-confirmed engine-state victory detection.
            # ---------------------------------------------------------------
            # Poll this before the playable-room gate: aEnding becomes active
            # precisely when gameplay is transitioning away, so RoomRecords
            # may already be unavailable.
            if (
                not ctx.runtime_goal_reached
                and ctx.memory.aending_active()
            ):
                ctx.runtime_goal_reached = True
                logger.info(
                    "RE1R: active aEnding engine state detected."
                )

            await ctx.sync_runtime_goal()

            # ---------------------------------------------------------------
            # Determine whether a playable RoomRecord set is active.
            # ---------------------------------------------------------------
            loaded, state_root = ctx.memory.gameplay_loaded()
            if loaded and state_root is not None:
                # Final Fresh New Game g3 application before normal location
                # scanning/delivery resumes for the first playable room.
                ctx.apply_pending_fresh_group3_restore(state_root, final=True)

            # ---------------------------------------------------------------
            # Only scan locations / deliver items in a truly loaded room.
            # ---------------------------------------------------------------
            if not loaded or state_root is None:
                if was_loaded:
                    logger.info(
                        "RE1R: gameplay not currently loaded; "
                        "location scanning paused."
                    )

                was_loaded = False
                previous_bank = None

                # StateRoot can be temporarily unavailable while the process
                # itself is still alive. Keep the handle (and any active hook
                # block) in that case. Detach only after process exit.
                if not ctx.memory.process_alive():
                    ctx.memory.detach()
                    was_attached = False
                await asyncio.sleep(ctx.poll_interval)
                continue

            if not was_loaded:
                logger.info(
                    "RE1R: playable room detected; location scanning active."
                )
                was_loaded = True

            # ---------------------------------------------------------------
            # Guarded virtual Item Box hotkey
            # ---------------------------------------------------------------
            if ctx.item_box_hotkey_pressed():
                try:
                    if ctx.memory.open_itembox():
                        logger.info(
                            "RE1R: %s opened the native virtual Item Box.",
                            ctx.itembox_hotkey,
                        )
                    else:
                        stage_room = ctx.memory.get_primary_stage_room()
                        if stage_room is None:
                            room_text = "room unavailable"
                        else:
                            room_text = (
                                f"stage {stage_room[0]:X}, "
                                f"room {stage_room[1]:02X}"
                            )
                        logger.debug(
                            "RE1R: %s Item Box hotkey ignored (%s). "
                            "Virtual Item Box access is restricted to %s "
                            "(stage %X, room %02X) during ordinary gameplay.",
                            ctx.itembox_hotkey,
                            room_text,
                            VIRTUAL_ITEMBOX_ROOM_NAME,
                            VIRTUAL_ITEMBOX_STAGE,
                            VIRTUAL_ITEMBOX_ROOM,
                        )
                except Exception:
                    logger.exception(
                        "RE1R: native virtual Item Box open failed."
                    )

            # ---------------------------------------------------------------
            # World pickup hooks + AP ReceivedItems delivery
            # ---------------------------------------------------------------
            ctx.reconcile_pickup_hooks()

            # Avoid mutating Item Box storage while any modal UI is active.
            # Pending AP items will deliver immediately after ordinary gameplay
            # resumes.
            if ctx.memory.ordinary_gameplay_active():
                # Recovery is a separate replay queue and deliberately does not
                # rewind next_delivery_index. Finish it first so normal AP item
                # order cannot overtake items missing from the loaded save.
                recovery_complete = ctx.process_pending_recovery_items()
                if recovery_complete:
                    ctx.process_pending_received_items()

            # A native delivery fault disarms delivery; remove world hooks in
            # the same watcher iteration so no more locations can be consumed
            # while game-side reward delivery is unsafe.
            ctx.reconcile_pickup_hooks()

            # ---------------------------------------------------------------
            # One 64-byte persistent flag read
            # ---------------------------------------------------------------
            current_bank = ctx.memory.read_group3(state_root)

            if current_bank is None:
                if not ctx.memory.process_alive():
                    ctx.memory.detach()
                    was_attached = False
                was_loaded = False
                previous_bank = None
                await asyncio.sleep(0.5)
                continue

            # Initial room load/reload is a full synchronization.
            # During normal play, only rescan our whitelist when the 64-byte
            # bank changes.
            if previous_bank is None or current_bank != previous_bank:
                checked_flags = checked_flags_from_bank(
                    current_bank,
                    ctx.flag_to_name,
                )

                newly_observed = checked_flags - ctx.locally_observed_flags

                if newly_observed:
                    ctx.locally_observed_flags |= newly_observed

                    for flag in sorted(newly_observed):
                        logger.info(
                            "RE1R: observed g3:%d set - %s",
                            flag,
                            ctx.flag_to_name[flag],
                        )

                # Full sync is intentionally safe to call repeatedly.
                await ctx.sync_observed_checks()

                previous_bank = current_bank

            # If we connected/reconnected to AP without a bank change,
            # watcher_event wakes us so previously observed flags can be
            # resubmitted. Runtime goal sync is also duplicate-safe.
            if ctx.watcher_event.is_set():
                ctx.watcher_event.clear()
                await ctx.sync_observed_checks()
                await ctx.sync_runtime_goal()

            await asyncio.sleep(ctx.poll_interval)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("RE1R: memory watcher error.")
            if not ctx.memory.process_alive():
                ctx.memory.detach()
                was_attached = False
            was_loaded = False
            previous_bank = None
            await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def make_re1r_gui(ctx: RE1RContext) -> None:
    from kvui import GameManager

    class RE1RManager(GameManager):
        logging_pairs = [
            ("Client", "Archipelago"),
        ]
        base_title = "Archipelago Resident Evil 1 Remake Client"

    ctx.ui = RE1RManager(ctx)
    ctx.ui_task = asyncio.create_task(
        ctx.ui.async_run(),
        name="UI",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args) -> None:
    location_file = Path(args.locations).expanduser().resolve()

    if not location_file.exists():
        raise FileNotFoundError(
            f"RE1R location catalog not found: {location_file}"
        )

    ctx = RE1RContext(
        args.connect,
        args.password,
        location_file=location_file,
        poll_interval=args.poll_interval,
        replay_received_items=args.replay_received_items,
        itembox_hotkey=args.itembox_key,
        save_tracking_file=Path(args.save_tracking_file).expanduser().resolve(),
    )
    ctx.auth = args.name

    logger.info(
        "RE1R: loaded %d randomizable locations from %s.",
        len(ctx.flag_to_name),
        location_file,
    )
    logger.info(
        "RE1R: waiting for %s. Location scanning begins only after a "
        "playable room is loaded.",
        PROCESS_NAME,
    )

    logger.info(
        "RE1R: waiting for real item-delivery metadata from the connected "
        "seed (RE1R APWorld 0.3.2+)."
    )

    logger.info(
        "RE1R: successful typewriter saves will record AP delivery cursors in %s.",
        ctx.save_tracking_file,
    )

    logger.info(
        "RE1R: AP deliveries are Item Box-only. Press %s in %s "
        "(stage %X, room %02X) during ordinary gameplay to open the "
        "native shared Item Box.",
        ctx.itembox_hotkey,
        VIRTUAL_ITEMBOX_ROOM_NAME,
        VIRTUAL_ITEMBOX_STAGE,
        VIRTUAL_ITEMBOX_ROOM,
    )

    if ctx.replay_received_items:
        logger.warning(
            "RE1R: replay mode requested. Once the connected seed's item map "
            "is validated, delivery will arm from ReceivedItems index 0."
        )
    else:
        logger.info(
            "RE1R: randomizer starts DISARMED for safety. After connecting "
            "to the fresh 0.3.0+ seed, use /re1rarm to skip existing "
            "ReceivedItems, activate pickup hooks, and deliver only newly "
            "received items."
        )

    ctx.server_task = asyncio.create_task(
        server_loop(ctx),
        name="server loop",
    )

    if gui_enabled and not getattr(args, "nogui", False):
        make_re1r_gui(ctx)

    ctx.run_cli()

    watcher = asyncio.create_task(
        game_watcher(ctx),
        name="RE1R memory watcher",
    )

    await ctx.exit_event.wait()

    ctx.server_address = None
    watcher.cancel()

    try:
        await watcher
    except asyncio.CancelledError:
        pass

    if ctx.memory.save_tracking_hooks_installed:
        try:
            ctx.memory.restore_save_tracking_hooks()
            logger.info(
                "RE1R: save-tracking hooks restored during client shutdown."
            )
        except Exception:
            logger.exception(
                "RE1R: failed to restore save-tracking hooks during shutdown. "
                "Close bhd.exe before continuing."
            )

    if ctx.memory.pickup_hooks_installed:
        try:
            ctx.memory.restore_pickup_hooks()
            logger.info("RE1R: pickup hooks restored during client shutdown.")
        except Exception:
            logger.exception(
                "RE1R: failed to restore pickup hooks during shutdown. "
                "Close bhd.exe before continuing."
            )

    ctx.memory.detach()
    await ctx.shutdown()


if __name__ == "__main__":
    import colorama

    parser = get_base_parser(
        description="Resident Evil 1 Remake Archipelago Client"
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Archipelago slot name.",
    )
    parser.add_argument(
        "--locations",
        default=str(DEFAULT_LOCATION_FILE),
        help=(
            "Path to RE1R_Jill_Normal_locations_minimal.json "
            "(default: next to this client)."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="Memory polling interval in seconds (default: 0.10).",
    )
    parser.add_argument(
        "--itembox-key",
        default=DEFAULT_ITEMBOX_HOTKEY,
        choices=sorted(FUNCTION_KEY_VKS),
        type=str.upper,
        help=(
            "Function key used to open the virtual native Item Box "
            f"(default: {DEFAULT_ITEMBOX_HOTKEY})."
        ),
    )
    parser.add_argument(
        "--save-tracking-file",
        default=str(DEFAULT_SAVE_TRACKING_FILE),
        help=(
            "JSON sidecar used to record the AP delivery cursor for each "
            "successful RE1R save slot (default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--replay-received-items",
        action="store_true",
        help=(
            "After a valid 0.3.0+ seed item map is received, arm from "
            "ReceivedItems index 0, including AP pickup hooks. Without this "
            "flag, AP delivery and gameplay-changing pickup hooks start "
            "disarmed; /re1rarm arms from the current end of ReceivedItems. "
            "Save-tracking instrumentation is independent."
        ),
    )

    args = parser.parse_args()

    if args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than zero.")

    colorama.just_fix_windows_console()
    asyncio.run(main(args))
