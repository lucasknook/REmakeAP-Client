#include <unordered_map>
#include <string>
#include <mutex>

#include <algorithm>
#include "pendingRequests.h"

using std::string;
using std::unordered_map;

namespace {
	std::mutex pendingRequestsMutex;
	unordered_map<uint64_t, string> pendingRequests;
}

void AddPendingRequest(uint64_t requestId, string callbackName) {
	std::lock_guard lock(pendingRequestsMutex);
	
	pendingRequests[requestId] = callbackName;
	return;
}

string PopPendingRequest(uint64_t requestId) {
	std::lock_guard lock(pendingRequestsMutex);
	auto pREntry = pendingRequests.find(requestId);
	if (pREntry == pendingRequests.end()) {
		return "";
	}
	else {
		string callbackName = pREntry->second;
		pendingRequests.erase(pREntry);
		return callbackName;
	}
}

void ClearPendingRequests() {
	std::lock_guard lock(pendingRequestsMutex);
	pendingRequests.clear();
	return;
}