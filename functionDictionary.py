functionDictionary = {
    #Requests
    "requestHandshake": {},
    #Responses
    #Events
    "error": {
        "errorId": int
    },
    "handshakeConfirmed": {},
    "print": {
        "messageToCopy": str
    }
}

def VerifyFunctionArguments(functionName: str, argsJson: object) -> bool:
    if not isinstance(argsJson, dict):
        return False

    fDEntry = functionDictionary.get(functionName)

    if fDEntry is None:
        return False

    if fDEntry == {}:
        return True

    if argsJson.keys() != fDEntry.keys():
        return False

    for name, type in fDEntry.items():
        if not isinstance(argsJson[name], type):
            return False

    return True