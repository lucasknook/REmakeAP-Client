#include "external/nlohmann/json.hpp"

#pragma once

bool VerifyFunctionArguments(std::string functionName, nlohmann::json args);
