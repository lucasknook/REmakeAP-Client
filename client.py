import socket
import json
import threading
from enum import Enum

from functionDictionary import VerifyFunctionArguments

TITLE = "RE1 Archipelago Client"
VERSION = "0.0.1"

attemptConnection = True

class State(Enum):
    DISCONNECTED = 1
    CONNECTING = 2
    CONNECTED = 3

state = State.DISCONNECTED
dllServer = None

def main():
    global dllServer
    global attemptConnection
    while True:
        if not attemptConnection:
            break

        dllServer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        aCThread = threading.Thread(target=AttemptConnection)
        aCThread.start()
        aCThread.join()

        rMThread = threading.Thread(target=ReceiveMessages)
        rMThread.start()
        rMThread.join()
        dllServer.close()

def AttemptConnection() -> bool:
    global state
    while True:
        try:
            dllServer.connect(("127.0.0.1", 52451))
            state = State.CONNECTING
            return
        except:
            pass

def ReceiveMessages():
    # Continuously append received bytes to receivedMessages
    receivedMessages = ""
    while True:
        try:
            message = dllServer.recv(1024).decode("utf-8")
            if message == "":
                print("Server closed connection")
                InitiateDisconnection()
                break
            receivedMessages += message

            #If a terminating newline is found, process the command
            while '\n' in receivedMessages:
                index = receivedMessages.index('\n')
                fullMessage = receivedMessages[:index]
                receivedMessages = receivedMessages[index + 1:]
                decodedMessage = DecodeMessage(fullMessage)
                if decodedMessage is None:
                    print("Invalid Json received")
                    continue
                ProcessCommand(decodedMessage)
        except OSError as error:
            print("Error maintaining connection to server: " + str(error))
            InitiateDisconnection()
            break

def ProcessCommand(command):
    global state
    match state:
        #Valid Connecting Commands
        case State.CONNECTING:
            match command["messageType"]:
                # REQUESTS
                case "request":
                    if command["requestType"] is None:
                        print("Invalid Json received")
                        return
                    match command["requestType"]:
                        case "requestHandshake":
                            SendHandshake(command["requestId"])
                        case _:
                            print(f"Invalid requestType {command['requestType']}")
                # EVENTS
                case "event":
                    if command["eventType"] is None:
                        print("Invalid Json received")
                        return
                    match command["eventType"]:
                        case "error":
                            if not VerifyFunctionArguments("error", command["args"]):
                                print("Invalid Json received")
                                return
                            print(f"Error {command['args']['errorId']}")
                        case "handshakeConfirmed":
                            if not VerifyFunctionArguments("handshakeConfirmed", command["args"]):
                                print("Invalid Json received")
                                return
                            state = State.CONNECTED
                            print("Successfully connected to DLL Server")
                            #Immediately send Repeat command
                            repeatCommand = {
                                    "messageType": "event",
                                    "eventType": "repeat",
                                    "args": {
                                        "messageToCopy": "Example repeat"
                                    }
                                }
                            SendMessage(repeatCommand)
                        case _:
                            print(f"Invalid eventType {command['eventType']}")
                case _:
                    print(f"Invalid messageType {command['messageType']}")
                    return
        #Valid Connected Commands
        case State.CONNECTED:
            match command["messageType"]:
                # REQUESTS
                case "request":
                    if command["requestType"] is None:
                        print("Invalid Json received")
                        return
                    match command["requestType"]:
                        case _:
                            print(f"Invalid requestType {command['requestType']}")
                # EVENTS
                case "event":
                    if command["eventType"] is None:
                        print("Invalid Json received")
                        return
                    match command["eventType"]:
                        case "print":
                            if not VerifyFunctionArguments("print", command["args"]):
                                print("Invalid Json received")
                                return
                            print(f"{command['args']['messageToCopy']}")
                        case _:
                            print(f"Invalid eventType {command['eventType']}")
                case _:
                    print(f"Invalid messageType {command['messageType']}")
                    return

def InitiateDisconnection():
    global state
    if state == State.DISCONNECTED:
        return
    dllServer.shutdown(socket.SHUT_RDWR)
    state = State.DISCONNECTED
    return

def SendMessage(pythonDict):
    jsonConversion = json.dumps(pythonDict) + '\n'
    try:
        dllServer.sendall(jsonConversion.encode('utf-8'))
    except Exception as e:
        print("Error maintaining connection to server: " + str(e))
        InitiateDisconnection()
        

def DecodeMessage(message):
    try:
        decoded = json.loads(message)
        return decoded
    except:
        return None

# Commands
def SendHandshake(requestId: int):
    requestedHandshakeJson = {
        "messageType": "response",
        "responseId": requestId,
        "args": {
            "title": TITLE,
            "version": VERSION
        }
    }
    SendMessage(requestedHandshakeJson)

def PrintError(errorId: int):
    global attemptConnection
    match errorId:
        case 1001:
            print("Error 1001: Outdated Client Version")
            attemptConnection = False
        case 1002:
            print("Error 1002: Outdated DLL Version")
            attemptConnection = False
        case 1003:
            print("Error 1003: Malformed Message")
        case 1004:
            print("Error 1004: Illegal Message Type")
        case 1005:
            print("Error 1005: Illegal Event Type")
        case 1006:
            print("Error 1006: Illegal Request Type")
        case 1007:
            print("Error 1007: DLL Not Accepting Messages")
    

if __name__ == "__main__":
    main()