#include <unordered_map>
#include <vector>
#include <algorithm>
#include <string>
#include "functionDictionary.h"
#include "external/nlohmann/json.hpp"

using json = nlohmann::json;
using std::string;
using std::vector;
using std::unordered_map;

static const unordered_map<string, vector<string>> functionDictionary = {
	//Events
	{"repeat", {"messageToCopy"}},

	//Responses
	{"handshake", {"version", "title"}}
};

bool VerifyFunctionArguments(string functionName, json argsJson) {
	
	//Convert to String Array
	if (!argsJson.is_object()) {
		return false;
	}
	vector<string> args;
	for (auto& entry : argsJson.items()) {
		args.push_back(entry.key());
	}

	//Validate
	auto fDEntry = functionDictionary.find(functionName);
	if (fDEntry == functionDictionary.end()) {
		return false;
	}
	vector<string> correctArgs = fDEntry->second;
	if (args.size() == 0 and correctArgs.size() != 0) {
		return false;
	}
	if (args.size() != correctArgs.size()) {
		return false;
	}
	for (int  i = 0; i < args.size(); i++) {
		if (count(args.begin(), args.end(), correctArgs[i]) == 0) {
			return false;
		}
	}
	return true;
}