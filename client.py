import socket
import json
import threading
import asyncio
import sys
import time
from pathlib import Path
from enum import Enum

ARCHIPELAGO_ROOT = Path(__file__).resolve().parent / "archipelago-source"
sys.path.insert(0, str(ARCHIPELAGO_ROOT))

from CommonClient import CommonContext
from functionDictionary import VerifyFunctionArguments

TITLE = "RE1 Archipelago Client"
VERSION = "0.0.1"

# Local TCP bridge exposed by the RE1R DLL.
DLL_HOST = "127.0.0.1"
DLL_PORT = 52451
DLL_RECONNECT_DELAY_SECONDS = 1.0

# Archipelago room settings. Set AP_SERVER_ADDRESS to an empty string to disable Archipelago.
AP_SERVER_ADDRESS = ""
AP_SLOT_NAME = ""
AP_PASSWORD = ""

class State(Enum):
    DISCONNECTED = 1
    CONNECTING = 2
    CONNECTED = 3

class RE1RContext(CommonContext):
    tags = CommonContext.tags | {"DeathLink", "TextOnly"}
    game = ""
    items_handling = 0b111
    want_slot_data = False

    async def server_auth(self, password_requested=False):
        if password_requested and not self.password:
            await super().server_auth(password_requested)

        await self.get_username()
        await self.send_connect(game="")

    def on_deathlink(self, data):
        super().on_deathlink(data)
        ForwardIncomingDeathLink(data)


ap_context = None
ap_loop = None


async def ArchipelagoMain(address, username, password):
    global ap_context, ap_loop

    ap_loop = asyncio.get_running_loop()

    context = RE1RContext()
    context.username = username
    context.password = password or None

    ap_context = context

    print(f"Connecting to Archipelago at {address}...")

    await context.connect(address)
    context.run_cli()

    try:
        await context.exit_event.wait()
    finally:
        await context.shutdown()


def RunArchipelago(address, username, password):
    try:
        asyncio.run(
            ArchipelagoMain(address, username, password)
        )
    except Exception as error:
        print(f"Archipelago connection error: {error}")

state = State.DISCONNECTED
dll_bridge = None


class DLLBridge:
    """Owns the DLL TCP socket, receive loop, and reconnect lifecycle."""

    def __init__(self, host, port, retry_delay=1.0):
        self.host = host
        self.port = port
        self.retry_delay = retry_delay
        self._socket = None
        self._send_lock = threading.Lock()
        self._stop_event = threading.Event()

    @property
    def connected(self):
        with self._send_lock:
            return self._socket is not None

    def run_forever(self):
        """Connect, process messages, and retry after a disconnect."""
        global state

        while not self._stop_event.is_set():
            sock = None
            try:
                print(f"Connecting to DLL bridge at {self.host}:{self.port}...")
                sock = socket.create_connection((self.host, self.port), timeout=5)
                sock.settimeout(None)
                with self._send_lock:
                    self._socket = sock

                state = State.CONNECTING
                print("Connected to DLL bridge; waiting for handshake.")
                ReceiveMessages(sock)

            except OSError as error:
                if not self._stop_event.is_set():
                    print(f"DLL bridge connection ended: {error}")
            finally:
                self._close_socket(sock)
                state = State.DISCONNECTED

            if not self._stop_event.is_set():
                print(f"Retrying DLL connection in {self.retry_delay:g}s...")
                self._stop_event.wait(self.retry_delay)

    def send(self, message):
        """Send one newline-delimited JSON message on the active connection."""
        encoded = (json.dumps(message) + "\n").encode("utf-8")
        with self._send_lock:
            sock = self._socket
            if sock is None:
                raise OSError("DLL bridge is not connected")
            sock.sendall(encoded)

    def disconnect(self):
        """Close the current connection, causing its receive loop to exit."""
        with self._send_lock:
            sock = self._socket
        self._close_socket(sock)

    def stop(self):
        self._stop_event.set()
        self.disconnect()

    def _close_socket(self, sock):
        if sock is None:
            return
        with self._send_lock:
            if self._socket is sock:
                self._socket = None
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def main():
    global dll_bridge

    print("=== RE1R Archipelago Client ===")
    if AP_SERVER_ADDRESS and AP_SLOT_NAME:
        ap_thread = threading.Thread(
            target=RunArchipelago,
            args=(AP_SERVER_ADDRESS, AP_SLOT_NAME, AP_PASSWORD),
            daemon=True
        )
        ap_thread.start()
        print("Archipelago connection started.")
    else:
        print("Archipelago disabled: configure AP_SERVER_ADDRESS and AP_SLOT_NAME to enable it.")

    dll_bridge = DLLBridge(DLL_HOST, DLL_PORT, DLL_RECONNECT_DELAY_SECONDS)
    try:
        dll_bridge.run_forever()
    except KeyboardInterrupt:
        print("Stopping RE1R client...")
    finally:
        dll_bridge.stop()
        print("DLL bridge stopped.")


def ReceiveMessages(sock):
    """Read newline-delimited JSON messages from one DLL connection."""
    received = b""
    while True:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                print("DLL closed the connection.")
                return
            received += chunk

            while b"\n" in received:
                raw_message, received = received.split(b"\n", 1)
                if not raw_message.strip():
                    continue
                try:
                    full_message = raw_message.decode("utf-8")
                except UnicodeDecodeError as error:
                    print(f"Ignoring non-UTF-8 DLL message: {error}")
                    continue

                decoded_message = DecodeMessage(full_message)
                if decoded_message is None:
                    print("Ignoring malformed JSON received from DLL.")
                    continue
                try:
                    ProcessCommand(decoded_message)
                except (KeyError, TypeError, ValueError) as error:
                    print(f"Ignoring malformed DLL command: {error}")
        except OSError as error:
            print(f"DLL receive error: {error}")
            return


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
                            readPlayerStateCommand = {
                                "messageType": "event",
                                "eventType": "readPlayerState",
                                "args": {}
                            }
                            SendMessage(readPlayerStateCommand)
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
                        case "localDeath":
                            HandleLocalDeath()
                        case _:
                            print(f"Invalid eventType {command['eventType']}")
                case _:
                    print(f"Invalid messageType {command['messageType']}")
                    return

def InitiateDisconnection():
    global state
    state = State.DISCONNECTED
    if dll_bridge is not None:
        dll_bridge.disconnect()


def SendMessage(pythonDict):
    if dll_bridge is None:
        print("Cannot send message: DLL bridge has not started.")
        return
    try:
        dll_bridge.send(pythonDict)
    except OSError as error:
        print(f"Could not send message to DLL: {error}")
        InitiateDisconnection()


def DecodeMessage(message):
    try:
        decoded = json.loads(message)
        return decoded
    except (json.JSONDecodeError, TypeError):
        return None

# Archipelago -> DLL. The DLL must recognize "deathLinkReceived".
def ForwardIncomingDeathLink(data):
    source = str(data.get("source", "Unknown"))
    cause = str(data.get("cause", ""))

    if state != State.CONNECTED or dll_bridge is None or not dll_bridge.connected:
        print("Incoming DeathLink received, but the DLL is not connected; cannot forward it.")
        return

    SendMessage({
        "messageType": "event",
        "eventType": "deathLinkReceived",
        "args": {"source": source, "cause": cause}
    })
    print(f"Forwarded incoming DeathLink from {source} to the DLL.")


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

def HandleLocalDeath():
    print("Local death detected.")

    if ap_context is None or ap_loop is None:
        print("DeathLink not sent: Archipelago is not initialized.")
        return

    try:
        future = asyncio.run_coroutine_threadsafe(SendLocalDeath(), ap_loop)
        def report_send_error(completed):
            try:
                error = completed.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                print(f"DeathLink send error: {error}")
        future.add_done_callback(report_send_error)
    except RuntimeError as error:
        print(f"DeathLink not sent: Archipelago event loop is unavailable ({error}).")


async def SendLocalDeath():
    if (
        ap_context is None
        or ap_context.server is None
        or ap_context.slot is None
    ):
        print("DeathLink not sent: Archipelago is not connected to a slot.")
        return

    await ap_context.send_death("Died in Resident Evil 1.")
    print("DeathLink send requested.")



def PrintError(errorId: int):
    match errorId:
        case 1001:
            print("Error 1001: Outdated Client Version")
            if dll_bridge is not None:
                dll_bridge.stop()
        case 1002:
            print("Error 1002: Outdated DLL Version")
            if dll_bridge is not None:
                dll_bridge.stop()
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