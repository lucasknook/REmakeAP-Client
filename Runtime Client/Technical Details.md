# Resident Evil 1 Remake Archipelago Client v6.7

Technical README for the current external Python client for **Resident Evil 1 Remake (`bhd.exe`, 32-bit x86)**.

This document describes the implementation state of **RE1RClient v6.7**, not the APWorld logic in general.

## Evidence labels

Findings are described using the same evidence convention used during reverse engineering:

- **Runtime-confirmed** — observed directly in a running game.
- **Source-backed** — supported by Ghidra/decompiled game code or the current client source.
- **Strong inference** — very well supported, but not directly behavior-proven.
- **Design choice** — behavior intentionally chosen for the Archipelago client.

---

## 1. Current scope

Current supported test target:

- Jill
- Normal difficulty
- Fresh New Game baseline
- 186 active randomized locations
- 73 stable compact AP item IDs
- `bhd.exe`, 32-bit x86

The client expects a connected RE1R Archipelago seed whose `slot_data` contains the native AP-item-to-RE1R delivery mapping. v6.7's operational messages expect a modern RE1R APWorld build (0.3.2+).

Still intentionally deferred:

- Chris support
- Other Jill difficulties
- DeathLink
- Full late-attach/restarted-client recovery when gameplay was already running before v6.7 installed its lifecycle hooks

---

## 2. High-level architecture

v6.7 is an **external Python process** using `pymem` and Win32 process APIs.

It does **not**:

- inject a DLL;
- modify RE1R asset files on disk;
- replace game resources;

It **does**:

- read and write `bhd.exe` process memory;
- allocate small executable remote memory blocks/code caves;
- patch a small set of known x86 instruction sites with jumps/calls;
- invoke proven native RE1R functions from the remote process;
- use `CreateRemoteThread` for selected native calls;
- communicate with the Archipelago server;
- write a JSON sidecar for AP save cursors.

All executable addresses below are **RVAs relative to the current `bhd.exe` module base**, unless explicitly shown as absolute Ghidra VAs.

On normal shutdown/detach, the client attempts to restore every executable instruction site it patched before freeing its remote hook blocks.

---

## 3. Core runtime pointers and state

### Context

```text
Context = [bhd.exe + 0x97C9C0]
```

Useful fields:

```text
Context + 0xE472C = stage
Context + 0xE4730 = room
Context + 0x38     = carried inventory slots
Context + 0x88     = Item Box bank 0 base
```

### StateRoot

```text
StateRoot = [bhd.exe + 0x97CA2C]
```

Runtime RoomRecord array:

```text
StateRoot + 0x474 = count
StateRoot + 0x478 = capacity
StateRoot + 0x480 = RoomRecord** data
```

The client uses the RoomRecord array as a gameplay-loaded discriminator. Title/save-select/loading states can leave several global pointers readable, so merely having a valid Context is not sufficient.

### Persistent Group-3 flags

**Runtime-confirmed:** Group 3 is a 512-bit persistent room/event completion bank.

```text
Group-3 bank = StateRoot + 0x2DC
size         = 0x40 bytes
flags        = 0..511
```

Group 3 includes randomized physical pickups, but it is **not intrinsically a pickup-only bank**. Files/documents and other persistent events also use it. Therefore v6.7 only manipulates flags from the resolved randomized-location whitelist.

---

## 4. Location scanning and Archipelago checks

The client loads:

```text
RE1R_Jill_Normal_locations_minimal_v2.json
```

and resolves its randomized Group-3 flag catalog against the Archipelago data package.

Normal scanner behavior:

1. Wait until a real playable RoomRecord array is loaded.
2. Read the 64-byte Group-3 bank approximately every 100 ms by default.
3. Compare against the previous snapshot.
4. For every resolved randomized flag that is set, submit the corresponding Archipelago `LocationCheck`.
5. On room reload/reconnect, perform a full resync so already-set persistent flags cannot be missed.

Archipelago duplicate handling makes repeated check submission safe.

---

## 5. Randomized physical pickup suppression

The client uses two **runtime-proven** executable hooks so randomized world objects keep their normal RE1R completion behavior while their vanilla reward is suppressed.

### Hook A — inventory-capacity rejection bypass

```text
RVA 0xEEB04
```

Purpose:

- only for a current RoomRecord matching the AP randomized physical-pickup whitelist;
- bypass the vanilla inventory-capacity failure branch;
- allow the interaction to proceed even if Jill's carried inventory is full.

Non-whitelisted pickups retain vanilla behavior.

### Hook B — vanilla reward suppression

```text
RVA 0xEECC4
```

Purpose:

- identify the current AP-whitelisted physical pickup;
- skip the vanilla item-reward implementation;
- preserve the game's own pickup completion/finalization path.

That means the physical object can disappear and its persistent completion flag can still be set normally, while the reward itself comes through Archipelago.

### Safety gate

**Design choice:** gameplay-changing pickup hooks are not enabled immediately at client startup.

Without replay mode, v6.7 starts **DISARMED**. After connecting to a valid fresh seed, use:

```text
/re1rarm
```

This:

- sets the normal delivery cursor to the current end of `ReceivedItems`;
- skips any pre-existing received items;
- arms real AP delivery;
- allows the pickup hooks to become active once gameplay is loaded.

Alternatively:

```text
--replay-received-items
```

arms from `ReceivedItems` index 0 after the connected seed's item map is validated.

Save/load tracking hooks are separate and can install even while world-randomization delivery is disarmed.

---

## 6. Native AP item delivery

### Delivery mapping

The connected seed supplies a stable mapping:

```text
AP item ID -> { native RE1R item ID, quantity }
```

v6.7 validates this seed-authoritative map before arming real delivery.

### Item Box-only policy

**Design choice:** AP `ReceivedItems` are delivered **only to the native shared Item Box**.

They are never automatically inserted into Jill's carried inventory.

Reason: AP rewards must not consume carried inventory slots needed by forced vanilla/story items such as the Lock Pick or Signal Rockets.

### Native Item Box setter

**Runtime-confirmed:** native function:

```text
FUN_00430120
RVA 0x30120
```

Conceptual signature:

```c
SetItemBoxSlot(
    Context,
    bank,
    slot,
    item_id,
    quantity
)
```

Native Item Box layout:

```text
Context + 0x88 + bank*0x800 + slot*8
```

Current client uses:

```text
bank 0
256 slots
8 bytes per slot: { item_id, quantity }
```

### Delivery planning

Before changing game memory, v6.7 plans the **entire** item delivery.

Behavior:

- fill compatible existing stacks first;
- create new Item Box slots only as needed;
- never partially deliver one AP `ReceivedItems` entry;
- if the complete delivery cannot fit, leave that ReceivedItems entry pending and preserve ordering;
- verify Item Box memory after the native writes.

If a native delivery throws/fails in a way that could have produced a partial write, delivery is disarmed rather than risking duplication.

---

## 7. Virtual native Item Box access

v6.7 can open the real shared RE1R Item Box UI without a physical storage chest.

Default hotkey:

```text
F8
```

Configurable with:

```text
--itembox-key F1..F12
```

### Room restriction

The hotkey is accepted only in:

```text
Mansion - West Wing Outer Stairway
stage 1, room 0x12
```

and only during ordinary `aGame` gameplay mode 4.

### Runtime-proven native sequence

The virtual open reproduces the vanilla physical-box sequence:

```text
FUN_0048B740(Context, 7)
FUN_004939E0(UIManager, 5, 0, 0, 0, 0, 1)
```

Relevant RVAs:

```text
UI manager pointer   bhd.exe + 0x9815A4
FUN_0048B740         RVA 0x8B740
FUN_004939E0         RVA 0x939E0
```

This is the normal native Item Box UI and normal shared native storage. Transfers between Jill and the box use vanilla game behavior.

No Main Hall asset, physical Item Box model, or storage-object asset is required by the Python client.

---

## 8. Victory detection

The old Heliport/Signal-Rockets heuristic is no longer used.

**Runtime-confirmed semantic victory:** successful Jill endings transition into active `aEnding` state. Tyrant death transitions through `aInit`, not `aEnding`.

Root chain:

```text
[bhd.exe + 0x9E4248] -> sBhdMain*
[sBhdMain + 0x282A0] -> area manager*
[manager + 0x3824]   -> active state count
[manager + 0x382C+i*4] -> active state object
[state] == bhd.exe + 0x875190 -> aEnding
```

Validation RVAs:

```text
sBhdMain vtable      0x8811C8
area manager vtable  0x880860
aGame vtable         0x875340
aEnding vtable       0x875190
```

When `aEnding` is observed, the client sends:

```text
StatusUpdate: CLIENT_GOAL
```

once.

---

## 9. Save tracking

v6.7 tracks the AP delivery prefix represented by each successful native RE1R save slot.

### Semantic save-slot trigger

**Runtime-confirmed:** a confirmed typewriter save reaches:

```text
FUN_004303A0
RVA 0x303A0
```

exactly once.

At function entry:

```text
[ESP+4] = zero-based RE1R save slot, 0..7
```

Examples:

```text
visible slot 5 -> 4
visible slot 6 -> 5
visible slot 8 -> 7
```

Cancelling the save UI does not reach this function.

The client instruments the call site at:

```text
RVA 0x3007F
```

and atomically captures:

```text
RE1R slot
current effective AP delivery cursor
```

### Steam save completion

RE1R writes the actual Steam save file:

```text
biodata.bin
```

through `ISteamRemoteStorage::FileWrite` from:

```text
FUN_0083AA50
RVA 0x43AA50
```

Known writer call sites instrumented by v6.7:

```text
RVA 0x43A3EE   synchronous path
RVA 0x43A4AE   worker-thread/asynchronous path
```

Storage object:

```text
[bhd.exe + 0x9E5210]
```

Relevant result field:

```text
storage + 0x28
```

Observed semantics:

```text
0    = successful write
1..9 = error/failure states
```

The save cursor is committed to the sidecar **only after the matching write succeeds**.

### Save sidecar

Default path:

```text
%USERPROFILE%\RE1R_Archipelago_save_tracking.json
```

Override:

```text
--save-tracking-file PATH
```

The sidecar is keyed by:

```text
AP seed name
AP team
AP slot
```

with an individual cursor for each RE1R save slot.

Example shape:

```json
{
  "schema": 1,
  "profiles": {
    "<profile hash>": {
      "seed_name": "...",
      "ap_team": 0,
      "ap_slot": 1,
      "ap_slot_name": "RE1R_Player",
      "re1r_slots": {
        "5": {
          "display_slot": 6,
          "saved_delivery_cursor": 4,
          "last_saved_item_index": 3,
          "saved_at_unix": 1786918102.0
        }
      }
    }
  }
}
```

Writes use a temporary file followed by replace, rather than rewriting the target JSON in place.

---

## 10. Existing-save load detection and AP item recovery

### Semantic load trigger

**Runtime-confirmed:** load-state application uses:

```text
FUN_0042F2F0
RVA 0x2F2F0
```

At entry:

```text
[ESP+4] = 0..7  -> actual selected save-slot load
[ESP+4] = 10    -> populate/refresh all 8 save slots for UI
```

Cancelling back out of the save UI produces no selected-slot load event.

The selected-slot path copies the corresponding `0xDF20` save record from the staging save array into the working save record and applies that state back to live `Context`.

### Separate recovery cursor

When a tracked save slot is loaded:

```text
saved_cursor = cursor stored for that RE1R slot
target_cursor = global AP delivery cursor captured before the load
```

If:

```text
target_cursor > saved_cursor
```

v6.7 creates a recovery plan for:

```text
ReceivedItems[saved_cursor : target_cursor]
```

Those entries are redelivered into the native Item Box in their original order.

The normal Archipelago delivery cursor is **not rewound**. Recovery has its own cursor, preventing the client from pretending Archipelago resent the items.

This has been runtime-tested with:

```text
save -> receive AP items -> die -> return to menu -> load old save
```

and the post-save AP items are restored into the Item Box.

If an RE1R slot has no tracked AP save cursor for the current seed/team/AP slot, v6.7 does not guess; item recovery is skipped for that load.

---

## 11. Restoring already-checked randomized world locations

Loading an old RE1R save can otherwise resurrect randomized physical pickups that Archipelago already considers checked.

v6.7 treats the AP server's `locations_checked` set as authoritative for randomized locations.

The client continuously builds a 64-byte mask containing only Group-3 flags corresponding to resolved AP randomized locations that are already checked.

### Existing save loads

The load hook at `FUN_0042F2F0(slot)`:

1. runs the vanilla save-state application;
2. obtains the newly loaded `StateRoot`;
3. ORs the AP-authoritative randomized-location mask into `StateRoot+0x2DC`;
4. returns to the original caller.

This happens **before the selected load-state function returns**, rather than waiting for a later Python polling cycle.

No flags are cleared.

No non-whitelisted Group-3 story/event flags are intentionally added.

`FlagManager_SetFlag` was reverse-engineered and confirms that Group-3 set semantics are simply an OR into the corresponding bit of the `+0xE4` flag-manager bank, which resolves to `StateRoot+0x2DC`.

Result: a randomized pickup checked after the old RE1R save remains completed/gone after loading that save.

---

## 12. Fresh New Game recovery

v6.7 also covers the edge case:

```text
Fresh New Game
-> receive/check AP content
-> die before making any RE1R save
-> start another Fresh New Game
```

### Runtime-confirmed lifecycle ordering

```text
Fresh New Game:
    aGame constructor

Existing save load:
    FUN_0042F2F0(slot 0..7)
    -> aGame constructor
```

`aGame` constructor:

```text
FUN_00402DE0
RVA 0x2DE0
```

The selected-load hook sets a one-shot remote marker before vanilla load application returns.

When the next `aGame` constructor executes:

```text
pending load marker exists -> this is the loaded-save session
no pending load marker     -> this is a Fresh New Game session
```

This distinction is made inside the remote tracking block, avoiding a Python watcher race between the load event and constructor event.

### Fresh New Game baseline

A Fresh New Game is treated as an implicit RE1R state with:

```text
AP saved cursor = 0
```

Therefore, if `N` AP items had already been delivered before the reset, v6.7 queues:

```text
ReceivedItems[0:N]
```

for Item Box recovery.

### Fresh New Game checked-location restoration

The `aGame` constructor occurs early enough that the new game's persistent flag banks may still be initialized afterward.

Therefore v6.7 does **not** rely on a single flag OR at constructor time.

Instead it:

- publishes a semantic Fresh New Game event;
- repeatedly reapplies the AP checked-location Group-3 mask while the new session initializes;
- performs a final application once a playable RoomRecord set exists;
- then resumes normal location scanning.

This behavior has been runtime-tested successfully.

---

## 13. Effective cursor vs global cursor

v6.7 intentionally tracks two different AP cursor concepts.

### Global delivery cursor

```text
next_delivery_index
```

This is the furthest normal AP `ReceivedItems` entry already delivered during the current client session. It does not rewind during old-save recovery.

### Effective save-state cursor

During recovery, the currently loaded RE1R state may only represent a smaller AP prefix.

Conceptually:

```text
if recovery active:
    effective cursor = recovery.next_index
else:
    effective cursor = next_delivery_index
```

A typewriter save made **during an incomplete recovery** records this effective cursor rather than incorrectly claiming that the loaded RE1R state already contains every previously delivered AP item.

This is why the remote tracking block mirrors both a live/effective cursor and a global delivery cursor.

---

## 14. Remote tracking block

The save/load/New Game subsystem uses one shared remote memory block.

It stores, among other things:

```text
live/effective AP cursor
captured save slot
captured save cursor
save request/completion sequence counters
save completion result
loaded slot
pre-load global cursor
load sequence counter
global delivery cursor
pending-load-before-aGame marker
Fresh New Game cursor
Fresh New Game sequence counter
64-byte AP checked Group-3 mask
```

This design lets semantically related game events capture state **inside the target process at the exact instruction event**, instead of relying on a 100 ms Python polling loop to sample timing-sensitive values.

---

## 15. Executable hook/patch sites in v6.7

These are the primary `bhd.exe` instruction sites modified by the Python client.

| RVA | Purpose |
|---:|---|
| `0xEEB04` | AP pickup capacity-rejection bypass |
| `0xEECC4` | AP pickup vanilla-reward suppression |
| `0x3007F` | Capture confirmed typewriter save slot + AP cursor |
| `0x43A3EE` | Wrap synchronous `biodata.bin` writer completion |
| `0x43A4AE` | Wrap worker-thread `biodata.bin` writer completion |
| `0x2F2F0` | Selected save-slot load capture + checked-location restoration |
| `0x2DE0` | `aGame` constructor / Fresh New Game session detection |

The allocated code-cave addresses are dynamic and are not fixed RVAs.

The client verifies expected original bytes before applying patches and attempts to restore its own patches on normal shutdown.

### Native functions used without permanently replacing them

| RVA | Function/use |
|---:|---|
| `0x30120` | `SetItemBoxSlot` |
| `0x8C9F0` | `SetInventorySlot` — known/proven, but AP delivery policy does not use it automatically |
| `0x8B740` | clear Context flag used by vanilla Item Box opening |
| `0x939E0` | high-level native Item Box UI open |

---

## 16. Commands

### `/re1r`

Shows runtime status including:

- process attachment;
- gameplay loaded state;
- resolved catalog/location/item counts;
- delivery map readiness;
- armed/disarmed state;
- pickup hook state;
- save-hook state;
- next normal AP item index;
- recovery state;
- pending ReceivedItems count.

### `/re1rsaves`

Shows successful tracked AP cursors for RE1R save slots for the **current AP seed/team/slot**.

Example:

```text
RE1R: tracked saves: slot 5=cursor 10, slot 8=cursor 14
```

### `/re1rarm`

Arms the gameplay randomizer from the **current end** of `ReceivedItems`.

Existing received items are skipped. New AP rewards are delivered normally after arming.

---

## 17. Useful command-line options

```text
--connect HOST:PORT
--name AP_SLOT_NAME
--locations PATH
--poll-interval SECONDS
--itembox-key F1..F12
--save-tracking-file PATH
--replay-received-items
```

Default poll interval:

```text
0.10 seconds
```

Default virtual Item Box key:

```text
F8
```

Default sidecar:

```text
~/RE1R_Archipelago_save_tracking.json
```

`--replay-received-items` is intentionally different from `/re1rarm`: replay mode begins from ReceivedItems index 0, whereas `/re1rarm` skips the already-received prefix and arms from the current end.

---

## 18. Process restarts, reconnects, and current resilience boundary

v6.7 is designed to tolerate normal room transitions, title screens, AP reconnects, and game lifecycle transitions while the client is present.

The now-tested persistence flows include:

```text
save -> receive AP items -> die -> load old save
```

and:

```text
Fresh New Game -> receive/check AP content -> die before saving
-> start another Fresh New Game
```

### Important remaining limitation: late attach

If the Python client itself is started/restarted **after RE1R gameplay is already active**, it did not witness either:

```text
FUN_0042F2F0(slot)   selected-slot load
```

or:

```text
FUN_00402DE0         aGame construction
```

for the current session.

Therefore v6.7 cannot yet safely infer whether that already-running gameplay state originated from save slot 1..8 or from a Fresh New Game. It intentionally does not guess a recovery baseline in that situation.

This is the current target for the next reconnect/restart-resilience milestone.

---

## 19. Compatibility with separate DLL/asset work

The Python client does not hook the RE1R asset importer or modify Main Hall assets.

A separate DLL that injects an Item Box asset/object into the Main Hall is conceptually independent from v6.7 provided that it:

- does not patch the same executable RVAs listed above;
- does not overwrite the client's code caves;
- does not replace the same native functions in a way that changes their calling convention/behavior;
- does not assume `bhd.exe` executable code remains globally byte-for-byte untouched.

For collision checking, compare the DLL's planned hook addresses against the table in **Section 15**.

The Python client's virtual Item Box is UI/storage-based and does not require or spawn a physical asset.

---

## 20. Failure-safety behavior

Several parts of the client intentionally fail closed:

- Pickup hooks remain disabled until real AP delivery is armed and mappings are valid.
- An unmapped ReceivedItems entry pauses ordered delivery.
- A full Item Box pauses delivery rather than partially inserting an item.
- A potentially partial native-delivery failure disarms automatic delivery to avoid duplication.
- A failed native RE1R save does not advance the sidecar AP cursor.
- Loading a save slot with no tracked AP cursor does not guess which items belong in that save.
- Group-3 restoration only ORs AP-authoritative randomized-location flags; it does not clear game flags.
- Unknown/non-randomized pickups remain vanilla.

---

## 21. Current tested persistence model

The intended Archipelago model is monotonic even though RE1R saves are rewindable.

```text
Archipelago checked locations  = authoritative monotonic set
Archipelago ReceivedItems      = authoritative monotonic ordered stream
RE1R save slot                 = rewindable snapshot
```

When RE1R rewinds:

```text
old RE1R save
    + AP checked-location mask
    + replay of AP items received after that save cursor
    = synchronized current AP gameplay state
```

Fresh New Game uses the same model with an implicit saved cursor of zero.

This allows normal RE1R death/save/load behavior without making Archipelago locations or received items rewind with the game save.

---

## 22. Current next technical milestone

The next planned milestone after v6.7 is **late-attach/restarted-client session identification**.

Goal:

> If the Python client starts after gameplay is already running, determine which RE1R save slot (or Fresh New Game baseline) produced the current state, then safely reconstruct the correct AP recovery baseline.

Until that is solved, persistence is strongest when the client is already running before the New Game/load lifecycle event occurs.

---

## 23. Summary of implemented features

v6.7 currently provides:

- Archipelago connection through CommonClient;
- 186-location Jill Normal randomized Group-3 scanner;
- duplicate-safe `LocationChecks`;
- AP-whitelisted physical-pickup suppression hooks;
- full-inventory pickup interaction support for randomized locations;
- seed-authoritative AP item ID -> native RE1R delivery mapping;
- native Item Box-only AP reward delivery;
- 256-slot Item Box stacking/planning/readback verification;
- guarded F-key virtual native Item Box UI in West Wing Outer Stairway;
- semantic `aEnding` victory detection and `CLIENT_GOAL` reporting;
- semantic typewriter-save detection with exact zero-based RE1R slot capture;
- Steam `biodata.bin` write-success confirmation;
- per-seed/team/AP-slot JSON save cursor sidecar;
- semantic selected-save-slot load detection;
- automatic recovery of AP items received after an older RE1R save;
- automatic restoration of already-checked randomized world-location flags on load;
- Fresh New Game detection via `aGame` lifecycle ordering;
- cursor-0 recovery for the die-before-first-save/New Game edge case;
- Fresh New Game restoration of already-checked randomized world locations;
- hook restoration on normal shutdown;
- status/diagnostic commands for runtime and tracked saves.

