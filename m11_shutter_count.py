#!/usr/bin/env python3
"""
m11_shutter_count.py - read the shutter count and usage counters of a Leica M11
over USB.

Usage:
    python3 m11_shutter_count.py          print the camera's counters
    python3 m11_shutter_count.py --json   print the raw JSON reply
    python3 m11_shutter_count.py -v       also hex-dump every PTP container

Requirements:
    pip install pyusb
    brew install libusb        (macOS)

Turn the camera on and connect it with a USB-C data cable. On macOS the
system camera service (ptpcamerad) claims the camera automatically; the
script stops it so it can take the camera, and macOS restarts it on demand.
Close Image Capture, Photos, Lightroom or any other camera app first.

The script sends only read-only commands. Tested on firmware 2.6.0.
"""

import argparse
import json
import struct
import subprocess
import sys
import time

import usb.core
import usb.util

LEICA_VID = 0x1A98
M11_PID = 0x2081

CT_COMMAND = 1
CT_DATA = 2
CT_RESPONSE = 3
CT_EVENT = 4
HEADER = struct.Struct("<IHHI")  # length, type, code, transaction ID

OC_GET_DEVICE_INFO = 0x1001
OC_OPEN_SESSION = 0x1002
OC_CLOSE_SESSION = 0x1003
OC_LE_OPEN_LE_SESSION = 0x9005
OC_LE_CLOSE_LE_SESSION = 0x9006
OC_LE_GET_STATISTICS = 0x904D

# Only these opcodes can be sent.
ALLOWED_OPCODES = {
    OC_GET_DEVICE_INFO,
    OC_OPEN_SESSION,
    OC_CLOSE_SESSION,
    OC_LE_OPEN_LE_SESSION,
    OC_LE_CLOSE_LE_SESSION,
    OC_LE_GET_STATISTICS,
}

LE_SESSION_KEY = 0xFF55
SESSION_ID = 1

RC_OK = 0x2001
RC_SESSION_ALREADY_OPEN = 0x201E
RESPONSE_NAMES = {
    0x2001: "OK",
    0x2002: "General_Error",
    0x2003: "Session_Not_Open",
    0x2005: "Operation_Not_Supported",
    0x2019: "Device_Busy",
    0x201D: "Invalid_Parameter",
    0x201E: "Session_Already_Open",
}

READ_SIZE = 0x100000 + 512
TIMEOUT_MS = 10000


class PTPError(RuntimeError):
    pass


def rc_name(code):
    return f"0x{code:04x} {RESPONSE_NAMES.get(code, 'unknown')}"


def hexdump(label, buf, limit=96):
    print(f"    {label} ({len(buf)} B)", file=sys.stderr)
    for off in range(0, min(len(buf), limit), 16):
        row = buf[off:off + 16]
        hx = " ".join(f"{b:02x}" for b in row)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        print(f"      {off:04x}  {hx:<47}  {asc}", file=sys.stderr)
    if len(buf) > limit:
        print(f"      ... {len(buf) - limit} more bytes", file=sys.stderr)


# ----------------------------------------------------------------------
# USB
# ----------------------------------------------------------------------
def find_camera():
    """Return (device, interface, bulk_out, bulk_in, interrupt_in)."""
    dev = usb.core.find(idVendor=LEICA_VID)
    if dev is None:
        sys.exit("No Leica camera found. Turn the camera on and connect it "
                 "with a USB-C data cable.")
    if dev.idProduct != M11_PID:
        print(f"Warning: product ID 0x{dev.idProduct:04x} is not an M11 "
              f"(0x{M11_PID:04x}).", file=sys.stderr)

    try:
        cfg = dev.get_active_configuration()
    except usb.core.USBError:
        dev.set_configuration()
        cfg = dev.get_active_configuration()

    def ep_match(intf, direction, ep_type):
        return usb.util.find_descriptor(
            intf,
            custom_match=lambda e:
                usb.util.endpoint_direction(e.bEndpointAddress) == direction
                and usb.util.endpoint_type(e.bmAttributes) == ep_type)

    for intf in cfg:
        bulk_out = ep_match(intf, usb.util.ENDPOINT_OUT, usb.util.ENDPOINT_TYPE_BULK)
        bulk_in = ep_match(intf, usb.util.ENDPOINT_IN, usb.util.ENDPOINT_TYPE_BULK)
        intr_in = ep_match(intf, usb.util.ENDPOINT_IN, usb.util.ENDPOINT_TYPE_INTR)
        if bulk_out is not None and bulk_in is not None:
            return dev, intf, bulk_out, bulk_in, intr_in
    sys.exit("The camera has no PTP interface. Check the camera's USB mode.")


def claim(dev, intf):
    """Claim the interface, stopping ptpcamerad on macOS if it holds it."""
    n = intf.bInterfaceNumber
    try:
        if dev.is_kernel_driver_active(n):
            dev.detach_kernel_driver(n)
    except (NotImplementedError, usb.core.USBError):
        pass

    # ptpcamerad ignores SIGTERM and launchd restarts it, so kill it and
    # claim immediately, retrying if it wins the race.
    for _ in range(5):
        try:
            usb.util.claim_interface(dev, n)
            return
        except usb.core.USBError as e:
            err = e
        if sys.platform != "darwin":
            break
        subprocess.run(["pkill", "-9", "ptpcamerad"], check=False)
        time.sleep(0.1)
    sys.exit(f"Cannot claim the camera ({err}). Close Image Capture, Photos, "
             "Lightroom or other camera apps and try again.")


# ----------------------------------------------------------------------
# PTP
# ----------------------------------------------------------------------
class PTPCamera:
    def __init__(self, bulk_out, bulk_in, intr_in, verbose=False):
        self.bulk_out = bulk_out
        self.bulk_in = bulk_in
        self.intr_in = intr_in
        self.verbose = verbose
        self.session_open = False
        self.le_open = False
        self.next_tid = 1

    def _send(self, ctype, code, tid, payload=b""):
        pkt = HEADER.pack(HEADER.size + len(payload), ctype, code, tid) + payload
        if self.verbose:
            hexdump(f"-> type {ctype} code 0x{code:04x} tid {tid}", pkt)
        self.bulk_out.write(pkt, timeout=TIMEOUT_MS)

    def _recv(self):
        chunk = b""
        while not chunk:  # skip zero-length packets
            chunk = bytes(self.bulk_in.read(READ_SIZE, timeout=TIMEOUT_MS))
        length, ctype, code, tid = HEADER.unpack_from(chunk)
        buf = bytearray(chunk)
        while len(buf) < length:
            buf += bytes(self.bulk_in.read(READ_SIZE, timeout=TIMEOUT_MS))
        if self.verbose:
            hexdump(f"<- type {ctype} code 0x{code:04x} tid {tid}", bytes(buf))
        return ctype, code, tid, bytes(buf[HEADER.size:length])

    def transaction(self, opcode, params=(), data_in=False):
        """Send one command. Return (response code, response params, data)."""
        if opcode not in ALLOWED_OPCODES:
            raise PTPError(f"opcode 0x{opcode:04x} is not allowed")
        if len(params) > 5:
            raise PTPError("PTP allows at most 5 parameters")

        if self.session_open and opcode != OC_OPEN_SESSION:
            tid = self.next_tid
            self.next_tid += 1
        else:
            tid = 0

        self._send(CT_COMMAND, opcode, tid, struct.pack(f"<{len(params)}I", *params))

        data = None
        ctype, code, rtid, body = self._recv()
        if ctype == CT_DATA and data_in:
            data = body
            ctype, code, rtid, body = self._recv()

        if ctype != CT_RESPONSE:
            raise PTPError(f"expected a response container, got type {ctype}")
        if rtid != tid:
            raise PTPError(f"transaction ID mismatch: sent {tid}, got {rtid}")
        n = len(body) // 4
        return code, struct.unpack(f"<{n}I", body[:n * 4]), data

    def drain_events(self, timeout_ms=300):
        if self.intr_in is None:
            return
        while True:
            try:
                self.intr_in.read(self.intr_in.wMaxPacketSize, timeout=timeout_ms)
            except usb.core.USBError:
                return


def _ptp_string(buf, off):
    n = buf[off]
    off += 1
    text = buf[off:off + 2 * n].decode("utf-16-le", errors="replace").rstrip("\x00")
    return text, off + 2 * n


def _u16_array(buf, off):
    (n,) = struct.unpack_from("<I", buf, off)
    off += 4
    return list(struct.unpack_from(f"<{n}H", buf, off)), off + 2 * n


def parse_device_info(buf):
    info = {}
    off = 8
    _, off = _ptp_string(buf, off)  # vendor extension description
    off += 2  # functional mode
    for key in ("operations", "events", "device_props", "capture_formats", "image_formats"):
        info[key], off = _u16_array(buf, off)
    for key in ("manufacturer", "model", "device_version", "serial_number"):
        info[key], off = _ptp_string(buf, off)
    return info


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def read_statistics(cam):
    """Return (device info, statistics dict)."""
    code, _, data = cam.transaction(OC_GET_DEVICE_INFO, data_in=True)
    if code != RC_OK or data is None:
        raise PTPError(f"GetDeviceInfo failed: {rc_name(code)}")
    info = parse_device_info(data)

    code, _, _ = cam.transaction(OC_OPEN_SESSION, params=(SESSION_ID,))
    if code not in (RC_OK, RC_SESSION_ALREADY_OPEN):
        raise PTPError(f"OpenSession failed: {rc_name(code)}")
    cam.session_open = True

    code, _, _ = cam.transaction(OC_LE_OPEN_LE_SESSION, params=(LE_SESSION_KEY,))
    if code != RC_OK:
        raise PTPError(f"LEOpenLESession failed: {rc_name(code)}")
    cam.le_open = True
    cam.drain_events()

    code, _, data = cam.transaction(OC_LE_GET_STATISTICS, data_in=True)
    if code != RC_OK or not data:
        raise PTPError(f"LEGetStatistics failed: {rc_name(code)}")
    text = data.rstrip(b"\x00").decode("utf-8", errors="replace")
    try:
        stats = json.loads(text)
    except json.JSONDecodeError:
        raise PTPError(f"camera returned invalid JSON:\n{text}")
    return info, stats


def close(cam):
    try:
        if cam.le_open:
            cam.transaction(OC_LE_CLOSE_LE_SESSION, params=(LE_SESSION_KEY,))
            cam.drain_events()
        if cam.session_open:
            cam.transaction(OC_CLOSE_SESSION)
    except (usb.core.USBError, PTPError) as e:
        print(f"Warning: closing the session failed: {e}", file=sys.stderr)


def print_report(info, stats):
    print(f"{info['manufacturer']} {info['model']}")
    print(f"  Firmware:  {stats.get('Firmwareversion', info['device_version'])}")
    print(f"  Serial:    {info['serial_number']}")
    counters = stats.get("Counter", {})
    if not counters:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return
    print()
    width = max(len(k) for k in counters)
    for key, value in counters.items():
        print(f"  {key:<{width}}  {value:>10,}" if isinstance(value, int)
              else f"  {key:<{width}}  {value}")


def main():
    ap = argparse.ArgumentParser(
        description="Read the shutter count and usage counters of a Leica M11.")
    ap.add_argument("--json", action="store_true",
                    help="print the camera's raw JSON reply")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="hex-dump every PTP container to stderr")
    args = ap.parse_args()

    dev, intf, bulk_out, bulk_in, intr_in = find_camera()
    claim(dev, intf)
    cam = PTPCamera(bulk_out, bulk_in, intr_in, verbose=args.verbose)

    try:
        info, stats = read_statistics(cam)
    except (PTPError, usb.core.USBError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        close(cam)
        usb.util.release_interface(dev, intf.bInterfaceNumber)
        usb.util.dispose_resources(dev)

    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        print_report(info, stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
