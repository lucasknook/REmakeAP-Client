#pragma once

#include <Windows.h>
#include <cstdint>

namespace Player {
    // Address of the global pointer to the player structure
    constexpr uintptr_t PlayerPointerAddress = 0x00CF2F28;

    // Reads one byte from the candidate state address without modifying game memory.
    // Returns false if the module base or memory read is unavailable.
    bool TryReadPlayerState(uint8_t& value, uintptr_t& address);
    // Writes one byte to the player-state address. Returns false on a failed write.
    bool TryWritePlayerState(uint8_t value, uintptr_t& address);
    bool TryGetPlayerAddress(uintptr_t& playerAddress);
}

