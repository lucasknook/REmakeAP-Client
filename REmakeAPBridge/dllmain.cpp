#include <WinSock2.h>
#include <WS2tcpip.h>
#include <Windows.h>
#include <process.h>
#include <cstdint>
#include <cstdio>
#include <atomic>
#include <mutex>
#include "functionDictionary.h"
#include "game/player.h"
#include "pendingRequests.h"
#include "external/nlohmann/json.hpp"

using json = nlohmann::json;
using std::string;
using std::size_t;
using std::vector;

#pragma comment(lib, "WS2_32.lib")

#define TITLE "RE1 Archipelago Server"
#define VERSION "0.0.1"

#define EVENTMESSAGE "event"
#define REQUESTMESSAGE "request"
#define RESPONSEMESSAGE "response"

enum State {
    DISCONNECTED,
    CONFIRMINGVERSION,
    CONNECTED
};

//Errors
//Errors to send to client
const int OUTDATEDCLIENTVERSION = 1001;
const int OUTDATEDDLLVERSION = 1002;
const int MALFORMEDMESSAGE = 1003;
const int ILLEGALMESSAGETYPE = 1004;
const int ILLEGALEVENTTYPE = 1005;
const int ILLEGALREQUESTTYPE = 1006;
const int NOTACCEPTINGMESSAGES = 1007;
//Internal errors
const int LOSTCONNECTION = 2001;
const int FAILEDTOSEND = 2002;

//File-wide variables
namespace {
    string multiworldId{};
    SOCKET client = INVALID_SOCKET;
    std::atomic<State> state{ DISCONNECTED };
    std::atomic<uint64_t> requestIdCounter{ 1 };
	std::mutex sendMutex;
    // Suppresses the local-death event caused by applying an incoming DeathLink.
    std::atomic<bool> suppressNextLocalDeath{ false };
}

//Declare Methods
void InitializeConnection();
int SendJsonMessage(json message);
json DecodeMessage(string message);
json CreateErrorMessage(int error); 
unsigned int __stdcall ReceiverThread(LPVOID parameter);
void ProcessCommand(json command);
void SendErrorToClient(int error);
void InitiateDisconnection();
uint64_t GenerateRequestId();
void Handshake(string version, string title);
void RepeatMessage(string messageToCopy);
void ReadPlayerState();
void TriggerDeathLinkReceived(string source, string cause);
unsigned int __stdcall PlayerStateMonitorThread(LPVOID parameter);


unsigned int __stdcall ModThread(LPVOID parameter) {

    //Initialize WinSock2
    WSADATA data;
    int error = WSAStartup(MAKEWORD(2,2), &data);
    if (error != 0) {
        MessageBoxA(NULL, "WinSock2 failed to start.", "Fatal Error", MB_OK | MB_ICONERROR);
        return 1;
    }

    SOCKET server = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);

    if(server == INVALID_SOCKET) {
        MessageBoxA(NULL, "Failed to create socket.", "Fatal Error", MB_OK | MB_ICONERROR);
        return 1;
	}
    
    //Create address of "127.0.0.1:52451"
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(52451);
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

    //Bind socket
    bind(server, (sockaddr*)&address, sizeof(address));
    listen(server, 1);

    state = DISCONNECTED;

    //Wait for connection from Client
    while (true) {
        client = accept(server, NULL, NULL);
        state = CONFIRMINGVERSION;
        HANDLE receiverThread = (HANDLE)_beginthreadex(NULL, 0, ReceiverThread, NULL, 0, nullptr);
        InitializeConnection();

        //If connection ends
        DWORD receiverThreadResult = WaitForSingleObject(receiverThread, INFINITE);
        CloseHandle(receiverThread);
        closesocket(client);
        client = INVALID_SOCKET;
    }
    return 0;
}

unsigned int __stdcall ReceiverThread(LPVOID parameter) {
    //Thread for Receiving Commands
    string receivedMessages = "";
    while (true) {
        //Continuously append received bytes to receivedMessages
        char receivedBuffer[1024];
        int receivedBytes = recv(client, receivedBuffer, sizeof(receivedBuffer), 0);
        if (receivedBytes == 0 || receivedBytes == SOCKET_ERROR) {
            InitiateDisconnection();
            return 0;
        }
        receivedMessages.append(receivedBuffer, static_cast<size_t>(receivedBytes));

		//If a terminating newline is found, process the command
        while (receivedMessages.find('\n') != string::npos) {
            size_t index = receivedMessages.find('\n');
            string commandString = receivedMessages.substr(0, index);
            json command = DecodeMessage(commandString);
            if (!command.is_null()) ProcessCommand(command);
            receivedMessages = receivedMessages.substr(index+1);
        }
    }
    return 0;
}

void ProcessCommand(json command) {
    switch (state) {

    //Valid Confirming Version Commands
    case CONFIRMINGVERSION:
        //Event Messages
        if (command["messageType"] == EVENTMESSAGE) {
            SendErrorToClient(ILLEGALMESSAGETYPE);
        }
        //Request Messages
        else if (command["messageType"] == REQUESTMESSAGE) {
            SendErrorToClient(ILLEGALMESSAGETYPE);
		}
        //Response Messages
        else if (command["messageType"] == RESPONSEMESSAGE) {
            if (!command.contains("responseId") || !command["responseId"].is_number_unsigned()) {
                SendErrorToClient(MALFORMEDMESSAGE);
                return;
            }
            uint64_t requestId = command["responseId"].get<std::uint64_t>();
            string callbackCommand = PopPendingRequest(requestId);
            if (callbackCommand.empty()) {
                SendErrorToClient(MALFORMEDMESSAGE);
                return;
            }
            //Response Commands
            if (callbackCommand == "handshake") {
                if (!VerifyFunctionArguments("handshake", command["args"])) {
                    SendErrorToClient(MALFORMEDMESSAGE);
                    return;
                }
                Handshake(command["args"]["version"], command["args"]["title"]);
            }
        }
        else SendErrorToClient(MALFORMEDMESSAGE);
        break;
    //Valid Connected Commands
    case CONNECTED:
        //Event Messages
        if(command["messageType"] == EVENTMESSAGE) {
            if (!command.contains("eventType") || !command["eventType"].is_string()) {
                SendErrorToClient(MALFORMEDMESSAGE);
                return;
            }
            string eventType = command["eventType"];
            if (!VerifyFunctionArguments(eventType, command["args"])) {
                SendErrorToClient(MALFORMEDMESSAGE);
                return;
            }
            //Event Commands
            if (eventType == "repeat") {
                RepeatMessage(command["args"]["messageToCopy"]);
            }
            else if (eventType == "readPlayerState") {
                ReadPlayerState();
            }
            else if (eventType == "deathLinkReceived") {
                if (!command["args"]["source"].is_string() || !command["args"]["cause"].is_string()) {
                    SendErrorToClient(MALFORMEDMESSAGE);
                    return;
                }
                TriggerDeathLinkReceived(command["args"]["source"].get<string>(), command["args"]["cause"].get<string>());
            }
        }
        //Request Messages
        else if (command["messageType"] == REQUESTMESSAGE) {
            SendErrorToClient(ILLEGALMESSAGETYPE);
		}
		//Response Messages
        else if (command["messageType"] == RESPONSEMESSAGE) {
            SendErrorToClient(ILLEGALMESSAGETYPE);
		}
        else SendErrorToClient(ILLEGALMESSAGETYPE);
		break;
    default:
        SendErrorToClient(ILLEGALMESSAGETYPE);
        break;
    }
    return;
}

void InitializeConnection() {
    json handshakeRequest = {
        {"messageType", REQUESTMESSAGE},
        {"requestId", GenerateRequestId()},
        {"requestType", "requestHandshake"}
	};

    AddPendingRequest(handshakeRequest["requestId"], "handshake");
    SendJsonMessage(handshakeRequest);
	return;
}

void SendErrorToClient(int error) {
    json message = CreateErrorMessage(error);
    if (client != INVALID_SOCKET) SendJsonMessage(message);
    else {
        //Fail Silently
    }
    return;
}

int SendJsonMessage(json message) {
	std::lock_guard lock(sendMutex);
    string outgoing = message.dump();
    //Append newline terminator
    outgoing.push_back('\n');

    size_t size = outgoing.size();
    size_t sizeSent = 0;

    while (sizeSent < size) {
        int sent = send(client, outgoing.data() + sizeSent, size - sizeSent, 0);
        if (sent == SOCKET_ERROR || sent == 0) {
            InitiateDisconnection();
            return FAILEDTOSEND;
        }
        sizeSent += sent;
    }
    return 0;
}

void InitiateDisconnection() {
    if (state == DISCONNECTED) return;
    shutdown(client, SD_BOTH);
    state = DISCONNECTED;
    ClearPendingRequests();
    return;
}

json DecodeMessage(string message) {
    json messageJson = json::parse(message, nullptr, false);
    if (messageJson.is_discarded()) {
        SendErrorToClient(MALFORMEDMESSAGE);
        return nullptr;
    }
    if (!messageJson.contains("messageType") || !messageJson["messageType"].is_string()) {
        SendErrorToClient(MALFORMEDMESSAGE);
        return nullptr;
    }
    return messageJson;
}

json CreateErrorMessage(int error) {
    json errorJson = {
        {"messageType", "event"},
		{"eventType", "error"},
        {
            "args",
            {
                {"errorId", error}
            }
        }
    };
    return errorJson;
}

uint64_t GenerateRequestId() {
	return requestIdCounter.fetch_add(1);
}

// COMMANDS
// ========
void Handshake(string version, string title) {
    
    //Compare Versions
    //================
    string dllVersion = VERSION;
	string clientVersion = version;
    vector<string> dllVersionParts;
	vector<string> clientVersionParts;
    size_t pos;

    while((pos = dllVersion.find('.')) != string::npos) {
        dllVersionParts.push_back(dllVersion.substr(0, pos));
        dllVersion.erase(0, pos + 1);
    }
	dllVersionParts.push_back(dllVersion); //Add the last part
    while((pos = clientVersion.find('.')) != string::npos) {
        clientVersionParts.push_back(clientVersion.substr(0, pos));
        clientVersion.erase(0, pos + 1);
	}
	clientVersionParts.push_back(clientVersion); //Add the last part
    int comparison = 0;
    for (int i = 0; i < dllVersionParts.size(); i++) {
        int dllNum = stoi(dllVersionParts[i]);
		int clientNum = stoi(clientVersionParts[i]);
        if (dllNum < clientNum) {
            comparison = -1;
            break;
        }
        else if (dllNum > clientNum) {
            comparison = 1;
            break;
		}
    }
    if(comparison > 0) {
        SendErrorToClient(OUTDATEDCLIENTVERSION);
        InitiateDisconnection();
    }
    else if (comparison < 0) {
        SendErrorToClient(OUTDATEDDLLVERSION);
        InitiateDisconnection();
    }
    else {
        //Complete Connection
        //Tell Client Handshake Confirmed
        json confirmation = {
            {"messageType", "event"},
            {"eventType", "handshakeConfirmed"},
            {"args", json::object()}
		};
        SendJsonMessage(confirmation);
        state = CONNECTED;
        HANDLE monitorThread = reinterpret_cast<HANDLE>(
            _beginthreadex(
                nullptr,
                0,
                PlayerStateMonitorThread,
                nullptr,
                0,
                nullptr
            )
            );

        if (monitorThread != nullptr) {
            CloseHandle(monitorThread);
        }
	}
}

void RepeatMessage(string messageToCopy) {
    json printMessage = {
        {"messageType", "event"},
        {"eventType", "print"},
        {
            "args",
            {
                {"messageToCopy", "Repeated: " + messageToCopy}
            }
        }
    };
    SendJsonMessage(printMessage);
}

// Apply an incoming Archipelago DeathLink by setting the tested player-state byte to 4.
// The monitor suppresses the localDeath event caused by this write to avoid an echo.
void TriggerDeathLinkReceived(string source, string cause) {
    uint8_t currentState = 0;
    uintptr_t stateAddress = 0;

    if (!Player::TryReadPlayerState(currentState, stateAddress)) {
        json message = {
            {"messageType", EVENTMESSAGE},
            {"eventType", "print"},
            {"args", {{"messageToCopy", "Incoming DeathLink could not be applied: player-state address is unreadable."}}}
        };
        SendJsonMessage(message);
        return;
    }

    if (currentState == 4) {
        json message = {
            {"messageType", EVENTMESSAGE},
            {"eventType", "print"},
            {"args", {{"messageToCopy", "Incoming DeathLink received, but the player is already in the death state."}}}
        };
        SendJsonMessage(message);
        return;
    }

    // Arm suppression before writing so the monitor cannot report this as a local death.
    suppressNextLocalDeath.store(true);

    uintptr_t writtenAddress = 0;
    if (!Player::TryWritePlayerState(4, writtenAddress)) {
        suppressNextLocalDeath.store(false);
        json message = {
            {"messageType", EVENTMESSAGE},
            {"eventType", "print"},
            {"args", {{"messageToCopy", "Incoming DeathLink failed: could not write the player-state byte."}}}
        };
        SendJsonMessage(message);
        return;
    }

    string detail = "Incoming DeathLink from " + source + " applied.";
    if (!cause.empty()) {
        detail += " Cause: " + cause;
    }
    json message = {
        {"messageType", EVENTMESSAGE},
        {"eventType", "print"},
        {"args", {{"messageToCopy", detail}}}
    };
    SendJsonMessage(message);
}

// Read-only diagnostic for the candidate RE1 player-state address.
// This does not modify game memory.
void ReadPlayerState() {
    uint8_t playerState = 0;
    uintptr_t candidateAddress = 0;

    if (!Player::TryReadPlayerState(playerState, candidateAddress)) {
        json printMessage = {
            {"messageType", EVENTMESSAGE},
            {"eventType", "print"},
            {"args", {{"messageToCopy", "Player-state probe failed: candidate address was not readable."}}}
        };
        SendJsonMessage(printMessage);
        return;
    }

    json printMessage = {
        {"messageType", EVENTMESSAGE},
        {"eventType", "print"},
        {"args", {{"messageToCopy", "Player-state probe: value=" + std::to_string(static_cast<unsigned int>(playerState)) +
            ", address=0x" + [] (uintptr_t value) {
                char buffer[2 * sizeof(uintptr_t) + 1]{};
                sprintf_s(buffer, "%llX", static_cast<unsigned long long>(value));
                return string(buffer);
            }(candidateAddress)}}}
    };
    SendJsonMessage(printMessage);
}


unsigned int __stdcall PlayerStateMonitorThread(LPVOID parameter) {
    bool hasPreviousState = false;
    uint8_t previousState = 0;

    while (state.load() == CONNECTED) {
        uint8_t currentState = 0;
        uintptr_t stateAddress = 0;

        if (Player::TryReadPlayerState(currentState, stateAddress)) {
            if (!hasPreviousState || currentState != previousState) {
                // If the first sample already sees state 4, it may be an incoming
                // DeathLink applied before the monitor's first poll. Clear suppression
                // because this initial sample will not emit a localDeath event.
                if (!hasPreviousState && currentState == 4) {
                    suppressNextLocalDeath.exchange(false);
                }

                // Keep the existing state-change diagnostic.
                json message = {
                    {"messageType", EVENTMESSAGE},
                    {"eventType", "print"},
                    {"args", {
                        {"messageToCopy",
                            "Player state changed: " +
                            std::to_string(
                                static_cast<unsigned int>(currentState))}
                    }}
                };

                SendJsonMessage(message);

                // Notify the Python client when a new death is detected.
                if (hasPreviousState &&
                    currentState == 4 &&
                    previousState != 4) {

                    if (suppressNextLocalDeath.exchange(false)) {
                        json suppressedMessage = {
                            {"messageType", EVENTMESSAGE},
                            {"eventType", "print"},
                            {"args", {
                                {"messageToCopy", "Incoming DeathLink caused this death; local DeathLink echo suppressed."}
                            }}
                        };
                        SendJsonMessage(suppressedMessage);
                    }
                    else {
                        json deathMessage = {
                            {"messageType", EVENTMESSAGE},
                            {"eventType", "localDeath"},
                            {"args", {
                                {"messageToCopy", "LOCAL_DEATH"}
                            }}
                        };
                        SendJsonMessage(deathMessage);
                    }
                }

                previousState = currentState;
                hasPreviousState = true;
            }
        }

        Sleep(100);
    }

    return 0;
}

// PROXY CODE
// ==========

//Create DirectInput8Create function pointer
typedef HRESULT(__stdcall* DirectInput8Create_t)(HINSTANCE, DWORD, REFIID, LPVOID*, LPUNKNOWN);
DirectInput8Create_t RealDirectInput8Create = nullptr;

//Proxy call for DirectInput8Create
extern "C" HRESULT __stdcall DirectInput8Create(HINSTANCE hinst, DWORD dwVersion, REFIID riidltf, LPVOID* ppvOut, LPUNKNOWN punkOuter) {
    if (RealDirectInput8Create == nullptr) {
        //Create a real dinput8.dll, forward call to it
        char systemPath[MAX_PATH];
        GetSystemDirectoryA(systemPath, MAX_PATH);
        strcat_s(systemPath, "\\dinput8.dll");

        HMODULE realDll = LoadLibraryA(systemPath);
        if (realDll) {
            RealDirectInput8Create = (DirectInput8Create_t)GetProcAddress(realDll, "DirectInput8Create");
            //Create our thread the first time DirectInput8Create is called
            HANDLE threadHandle = (HANDLE)_beginthreadex(NULL, 0, ModThread, NULL, 0, nullptr);
            CloseHandle(threadHandle);
        }
        else {
            return E_FAIL;
        }
    }

    return RealDirectInput8Create(hinst, dwVersion, riidltf, ppvOut, punkOuter);
}

BOOL APIENTRY DllMain( HMODULE hModule,
                       DWORD  ul_reason_for_call,
                       LPVOID lpReserved
                     )
{
    return TRUE;
}