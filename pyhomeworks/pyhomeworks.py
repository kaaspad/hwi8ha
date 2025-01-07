"""Homeworks.

A partial implementation of an interface to series-4 and series-8 Lutron
Homeworks systems.

The Series4/8 is connected to an RS232 port to an Ethernet adaptor (NPort).

Michael Dubno - 2018 - New York
"""

from contextlib import suppress
import logging
import select
import socket
from threading import Thread
import time
from typing import Final

from . import exceptions

_LOGGER = logging.getLogger(__name__)


def _p_address(arg):    return arg
def _p_button(arg):     return int(arg)
def _p_enabled(arg):    return arg == "enabled"
def _p_level(arg):      return int(arg)
def _p_ledstate(arg):   return [int(num) for num in arg]

def _norm(x): return (x, _p_address, _p_button)


# Callback types
HW_BUTTON_DOUBLE_TAP = "button_double_tap"
HW_BUTTON_HOLD = "button_hold"
HW_BUTTON_PRESSED = "button_pressed"
HW_BUTTON_RELEASED = "button_released"
HW_KEYPAD_ENABLE_CHANGED = "keypad_enable_changed"
HW_KEYPAD_LED_CHANGED = "keypad_led_changed"
HW_LIGHT_CHANGED = "light_changed"
HW_LOGIN_INCORRECT = "login_incorrect"

ACTIONS = {
    "KBP":      _norm(HW_BUTTON_PRESSED),
    "KBR":      _norm(HW_BUTTON_RELEASED),
    "KBH":      _norm(HW_BUTTON_HOLD),
    "KBDT":     _norm(HW_BUTTON_DOUBLE_TAP),
    "DBP":      _norm(HW_BUTTON_PRESSED),
    "DBR":      _norm(HW_BUTTON_RELEASED),
    "DBH":      _norm(HW_BUTTON_HOLD),
    "DBDT":     _norm(HW_BUTTON_DOUBLE_TAP),
    "SVBP":     _norm(HW_BUTTON_PRESSED),
    "SVBR":     _norm(HW_BUTTON_RELEASED),
    "SVBH":     _norm(HW_BUTTON_HOLD),
    "SVBDT":    _norm(HW_BUTTON_DOUBLE_TAP),
    "KLS":      (HW_KEYPAD_LED_CHANGED, _p_address, _p_ledstate),
    "DL":       (HW_LIGHT_CHANGED, _p_address, _p_level),
    "KES":      (HW_KEYPAD_ENABLE_CHANGED, _p_address, _p_enabled),
}

IGNORED = {
    "Keypad button monitoring enabled",
    "GrafikEye scene monitoring enabled",
    "Dimmer level monitoring enabled",
    "Keypad led monitoring enabled",
}


class Homeworks(Thread):
    """Interface with a Lutron Homeworks 4/8 Series system."""

    _socket: socket.socket

    COMMAND_SEPARATOR: Final = b"\r\n"
    LOGIN_REQUEST: Final = b"LOGIN: "
    LOGIN_INCORRECT: Final =b"login incorrect"
    LOGIN_SUCCESSFUL: Final =b"login successful"
    PROMPT_REQUESTS: Final = [b"LNET> ", b"L232> "]
    POLLING_FREQ: Final = 1.
    LOGIN_PROMPT_WAIT_TIME: Final = 0.2
    SOCKET_CONNECT_TIMEOUT: Final = 10.0

    def __init__(self, host, port, callback, login=None):
        """Initialize."""
        Thread.__init__(self)
        self._host = host
        self._port = port
        self._login = login
        self._callback = callback
        self._socket = None

        self._running = False

    def connect(self):
        """Connect to controller using host, port."""
        self._connect(False)

    def _connect(self, callback_on_login_error):
        """Connect to controller using host, port."""
        try:
            self._socket = socket.create_connection((self._host, self._port), self.SOCKET_CONNECT_TIMEOUT)
        except (OSError, ValueError) as error:
            _LOGGER.debug("Failed to connect to %s:%s - %s", self._host, self._port, error, exc_info=True)
            raise exceptions.HomeworksConnectionFailed(f"Couldn't connect to '{self._host}:{self._port}'") from error

        _LOGGER.info("Connected to '%s:%s'", self._host, self._port)

        # Wait for login prompt
        time.sleep(self.LOGIN_PROMPT_WAIT_TIME)
        buffer = self._read()
        while buffer.startswith(self.COMMAND_SEPARATOR):
            buffer = buffer[len(self.COMMAND_SEPARATOR):]
        if buffer.startswith(self.LOGIN_REQUEST):
            try:
                self._handle_login_request(callback_on_login_error)
            except exceptions.HomeworksException:
                self._close()
                raise

        # Subscribe
        self._subscribe()

    def _read(self):
        readable, _, _ = select.select([self._socket], [], [], self.POLLING_FREQ)
        if not readable:
            return b""
        recv = self._socket.recv(1024)
        if not recv:
            self._close()
            raise exceptions.HomeworksConnectionLost
        _LOGGER.debug("recv: %s", recv)
        return recv

    def _send(self, command):
        _LOGGER.debug("send: %s", command)
        try:
            self._socket.send(command.encode("utf8") + self.COMMAND_SEPARATOR)
        except (ConnectionError, AttributeError):
            self._close()
            return False
        else:
            return True

    def fade_dim(self, intensity, fade_time, delay_time, addr):
        """Change the brightness of a light."""
        self._send(f"FADEDIM, {intensity}, {fade_time}, {delay_time}, {addr}")

    def request_dimmer_level(self, addr):
        """Request the controller to return brightness."""
        self._send(f"RDL, {addr}")

    def run(self):
        """Read and dispatch messages from the controller."""
        self._running = True
        buffer = b""
        while self._running:
            if self._socket is None:
                with suppress(exceptions.HomeworksException):
                    self._connect(True)
            else:
                try:
                    buffer += self._read()
                    while True:
                        (command, separator, remainder) = buffer.partition(self.COMMAND_SEPARATOR)
                        if separator != self.COMMAND_SEPARATOR:
                            break
                        buffer = remainder
                        if not command:
                            continue
                        self._processReceivedData(command)
                except (ConnectionError, AttributeError, exceptions.HomeworksConnectionLost):
                    _LOGGER.warning("Lost connection.")
                    self._close()
                    buffer = b""
                    if self._running:
                        time.sleep(self.POLLING_FREQ)

        self._running = False
        self._close()

    def _processReceivedData(self, data_b: bytes):
        _LOGGER.debug("Raw: %s", data_b)
        try:
            data = data_b.decode("utf-8")
        except UnicodeDecodeError:
            _LOGGER.warning("Invalid data: %s", data)
            return
        if data in IGNORED:
            return
        try:
            raw_args = data.split(", ")
            action = ACTIONS.get(raw_args[0], None)
            if action and len(raw_args) == len(action):
                args = [parser(arg) for parser, arg in
                        zip(action[1:], raw_args[1:])]
                self._callback(action[0], args)
            else:
                _LOGGER.warning("Not handling: %s", raw_args)
        except ValueError:
            _LOGGER.warning("Unexpected data: %s", data)

    def close(self):
        """Close the connection to the controller."""
        if self._running:
            raise exceptions.HomeworksException("Can't call close when thread is running")
        self._close()

    def _close(self):
        """Close the connection to the controller."""
        if self._socket:
            self._socket.close()
            self._socket = None

    def stop(self):
        """Wait for the worker thread to stop."""
        self._running = False
        self.join()

    def _handle_login_request(self, callback_on_login_error):
        if not self._login:
            raise exceptions.HomeworksNoCredentialsProvided
        self._send(self._login)

        buffer = self._read()
        while buffer.startswith(self.COMMAND_SEPARATOR):
            buffer = buffer[len(self.COMMAND_SEPARATOR):]
        if buffer.startswith(self.LOGIN_INCORRECT):
            if callback_on_login_error:
                self._callback(HW_LOGIN_INCORRECT, [])
            raise exceptions.HomeworksInvalidCredentialsProvided
        if buffer.startswith(self.LOGIN_SUCCESSFUL):
            _LOGGER.debug("Login successful")

    def _subscribe(self):
        # Setup interface and subscribe to events
        self._send("PROMPTOFF")  # No prompt is needed
        self._send("KBMON")  # Monitor keypad events
        self._send("GSMON")  # Monitor GRAFIKEYE scenes
        self._send("DLMON")  # Monitor dimmer levels
        self._send("KLMON")  # Monitor keypad LED states
