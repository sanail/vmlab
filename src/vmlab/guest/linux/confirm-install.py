"""Confirm an Ubuntu autoinstall from inside the installer's live session.

The Ubuntu installer (subiquity) asks for a click before it writes the disk
unless `autoinstall` is on the kernel command line, which vmlab cannot edit
without typing into GRUB. It takes the same confirmation over its local API,
so cloud-init starts this script in the live session: it waits for the
installer to ask, then confirms, as the Install button would. Progress goes
to the serial port, which vmlab writes to a file on the Host.
"""

import glob
import http.client
import json
import socket
import time

SOCKET = "/run/subiquity/socket"
TIMEOUT = 2 * 3600  # s, as long as vmlab waits for the install


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET)


def call(method, path):
    connection = Connection("localhost", timeout=10)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        connection.close()


def log(text):
    for path in glob.glob("/dev/ttyS0") + glob.glob("/dev/ttyAMA0"):
        try:
            with open(path, "w") as port:
                port.write("vmlab-install: %s\n" % text)
        except OSError:
            pass


def main():
    deadline = time.time() + TIMEOUT
    last = None
    while time.time() < deadline:
        try:
            status, body = call("GET", "/meta/status")
            state = json.loads(body).get("state") if status == 200 else "HTTP %s" % status
        except (OSError, ValueError) as exc:
            state = "no installer yet (%s)" % exc.__class__.__name__
        if state != last:
            log(state)
            last = state
        if state == "NEEDS_CONFIRMATION":
            log("confirming: %s" % (call("POST", '/meta/confirm?tty=%22%2Fdev%2Ftty1%22')[0],))
        if state in ("DONE", "ERROR", "EXITED"):
            return
        time.sleep(2)
    log("gave up after %ss" % TIMEOUT)


main()
