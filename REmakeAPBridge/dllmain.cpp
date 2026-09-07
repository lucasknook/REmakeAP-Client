#include <WinSock2.h>
#include <WS2tcpip.h>
#include <Windows.h>
#include <process.h>
#include <cstdint>
#include <atomic>
#include <mutex>
#include "functionDictionary.h"
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