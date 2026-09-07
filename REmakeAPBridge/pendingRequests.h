#include <string>
#include <cstdint>

#pragma once

void AddPendingRequest(uint64_t requestId, std::string callbackName);
std::string PopPendingRequest(uint64_t requestId);
void ClearPendingRequests();