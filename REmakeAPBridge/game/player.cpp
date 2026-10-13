#include "player.h"

bool Player::TryReadPlayerState(uint8_t& value, uintptr_t& address) {
    // GetModuleHandleW(nullptr) returns the base address of the current process executable (bhd.exe).
    const HMODULE executableModule = GetModuleHandleW(nullptr);
    if (executableModule == nullptr) {
        return false;
    }

    const uintptr_t moduleBase = reinterpret_cast<uintptr_t>(executableModule);
    
    uintptr_t playerAddress = 0;

    if (!Player::TryGetPlayerAddress(playerAddress)) {
        return false;
    }

    address = playerAddress + 0x2D;

    SIZE_T bytesRead = 0;
    const BOOL succeeded = ReadProcessMemory(
        GetCurrentProcess(),
        reinterpret_cast<LPCVOID>(address),
        &value,
        sizeof(value),
        &bytesRead
    );

    return succeeded != FALSE && bytesRead == sizeof(value);
}

bool Player::TryGetPlayerAddress(uintptr_t& playerAddress) {
    playerAddress = 0;

    SIZE_T bytesRead = 0;
    const BOOL succeeded = ReadProcessMemory(
        GetCurrentProcess(),
        reinterpret_cast<LPCVOID>(PlayerPointerAddress),
        &playerAddress,
        sizeof(playerAddress),
        &bytesRead
    );

    if (succeeded == FALSE || bytesRead != sizeof(playerAddress)) {
        playerAddress = 0;
        return false;
    }

    return playerAddress != 0;
}

bool Player::TryWritePlayerState(uint8_t value, uintptr_t& address) {
    uintptr_t playerAddress = 0;
    if (!Player::TryGetPlayerAddress(playerAddress)) {
        address = 0;
        return false;
    }

    address = playerAddress + 0x2D;

    SIZE_T bytesWritten = 0;
    const BOOL succeeded = WriteProcessMemory(
        GetCurrentProcess(),
        reinterpret_cast<LPVOID>(address),
        &value,
        sizeof(value),
        &bytesWritten
    );

    return succeeded != FALSE && bytesWritten == sizeof(value);
}