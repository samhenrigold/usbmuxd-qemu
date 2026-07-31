#!/usr/bin/env python3
"""
Fake iPod touch 2G for the usbmuxd QEMU backend.

Plays the role QEMU plays: dials the backend's listener and answers 5-byte
framed requests. Descriptors mirror the real device (05ac:1293, 3 configs, the
AppleUSBMux interface at 255/254/2). EP0 IN is deliberately chunked at 64 bytes
so the host's multi-chunk descriptor path gets exercised, and anything with
nothing to say NAKs, like the real endpoint model does.

Speaks just enough of the mux protocol to answer usbmuxd's version request
with a version 1 reply, which is what makes the device visible to clients.
"""
import socket
import struct
import sys
import time

HDR = struct.Struct("<BBBh")
F_SETUP, F_RESET, F_ENUMDONE = 1, 2, 4
DIR_IN = 0x80
NAK, STALL = -2, -3

EP0_CHUNK = 64
UDID = "b7e29a1f4c3d5e6a8b9c0d1e2f30415263748596"

DEVICE_DESC = bytes([
    0x12, 0x01, 0x00, 0x02, 0x00, 0x00, 0x00, 0x40,
    0xac, 0x05, 0x93, 0x12, 0x01, 0x00, 0x01, 0x02, 0x03, 0x03,
])


def iface(num, alt, neps, cls, sub, proto, istr=0):
    return bytes([9, 4, num, alt, neps, cls, sub, proto, istr])


def endp(addr, mps):
    return bytes([7, 5, addr, 0x02, mps & 0xff, mps >> 8, 0])


def config(value, ifaces):
    body = b"".join(ifaces)
    n = sum(1 for i in ifaces if i[1] == 4)
    total = 9 + len(body)
    return bytes([9, 2, total & 0xff, total >> 8, n, value, 0, 0xC0, 0]) + body


PTP = iface(0, 0, 3, 6, 1, 1) + endp(0x81, 512) + endp(0x02, 512) + endp(0x83, 64)
MUX = iface(1, 0, 2, 255, 254, 2) + endp(0x84, 512) + endp(0x05, 512)

CONFIGS = [
    config(1, [PTP]),                       # PTP only
    config(2, [PTP, MUX]),                  # PTP + Apple Mobile Device
    config(3, [iface(0, 0, 0, 255, 253, 1)]),
]

STRINGS = {
    0: bytes([4, 3, 0x09, 0x04]),
    1: None, 2: None, 3: None,
}


def mkstring(s):
    u = s.encode("utf-16-le")
    return bytes([len(u) + 2, 3]) + u


STRINGS[1] = mkstring("Apple Inc.")
STRINGS[2] = mkstring("iPod")
STRINGS[3] = mkstring(UDID)


class Device:
    def __init__(self, verbose=True):
        self.addr = 0
        self.config = 0
        self.ep0_in = b""
        self.zlp_in = False
        self.bulk_out = b""
        self.bulk_in = b""
        self.verbose = verbose
        self.enumdone = False

    def log(self, *a):
        if self.verbose:
            print("[dev]", *a, flush=True)

    # --- mux protocol ----------------------------------------------------
    def mux_rx(self, data):
        self.bulk_out += data
        while len(self.bulk_out) >= 8:
            proto, length = struct.unpack_from(">II", self.bulk_out, 0)
            if length < 8 or length > 65536:
                self.log("bogus mux header, resetting stream")
                self.bulk_out = b""
                return
            if len(self.bulk_out) < length:
                return
            pkt, self.bulk_out = self.bulk_out[:length], self.bulk_out[length:]
            if proto == 0:
                major, minor, _ = struct.unpack_from(">III", pkt, 8)
                self.log(f"mux version request v{major}.{minor} -> replying v1.0")
                vh = struct.pack(">III", 1, 0, 0)
                self.bulk_in += struct.pack(">II", 0, 8 + len(vh)) + vh
            else:
                self.log(f"mux packet proto={proto} len={length} (ignored)")

    # --- control ---------------------------------------------------------
    def setup(self, pkt):
        bmr, breq, wval, widx, wlen = struct.unpack("<BBHHH", pkt)
        self.ep0_in = b""
        self.zlp_in = False
        desc = None

        if bmr == 0x80 and breq == 0x06:
            dtype, didx = wval >> 8, wval & 0xff
            if dtype == 1:
                desc = DEVICE_DESC
            elif dtype == 2:
                desc = CONFIGS[didx] if didx < len(CONFIGS) else None
            elif dtype == 3:
                desc = STRINGS.get(didx)
            if desc is None:
                self.log(f"unsupported descriptor {dtype}/{didx} -> STALL")
                return STALL
            self.ep0_in = desc[:wlen]
            self.log(f"GET_DESCRIPTOR type={dtype} idx={didx} -> {len(self.ep0_in)} bytes")
        elif bmr == 0x00 and breq == 0x05:
            self.addr = wval & 0x7f
            self.zlp_in = True
            self.log(f"SET_ADDRESS {self.addr}")
        elif bmr == 0x00 and breq == 0x09:
            self.config = wval
            self.zlp_in = True
            self.log(f"SET_CONFIGURATION {self.config}")
        elif bmr == 0x01 and breq == 0x0b:
            self.zlp_in = True
            self.log(f"SET_INTERFACE {widx}/{wval}")
        else:
            self.log(f"unhandled request {bmr:#04x}/{breq:#04x} -> STALL")
            return STALL
        return 8

    # --- one transaction --------------------------------------------------
    def handle(self, ep, flags, length, payload):
        if flags & F_RESET:
            self.log("RESET")
            self.addr = 0
            self.config = 0
            self.ep0_in = b""
            return 0, b""
        if flags & F_ENUMDONE:
            self.log("ENUMDONE")
            self.enumdone = True
            return 0, b""

        num = ep & 0x7f
        if ep & DIR_IN:
            if num == 0:
                if self.ep0_in:
                    n = min(len(self.ep0_in), length, EP0_CHUNK)
                    data, self.ep0_in = self.ep0_in[:n], self.ep0_in[n:]
                    return n, data
                if self.zlp_in:
                    self.zlp_in = False
                    return 0, b""
                return NAK, b""
            if num == 4:
                if self.bulk_in:
                    n = min(len(self.bulk_in), length)
                    data, self.bulk_in = self.bulk_in[:n], self.bulk_in[n:]
                    self.log(f"bulk IN {n} bytes")
                    return n, data
                return NAK, b""
            return STALL, b""

        if num == 0:
            if flags & F_SETUP:
                if len(payload) != 8:
                    return STALL, b""
                return self.setup(payload), b""
            return 0, b""          # status stage
        if num == 5:
            self.log(f"bulk OUT {len(payload)} bytes")
            self.mux_rx(payload)
            return len(payload), b""
        return STALL, b""


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("host closed the connection")
        buf += chunk
    return buf


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 1235

    s = socket.create_connection((host, port))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print(f"[dev] connected to host bridge at {host}:{port}", flush=True)

    dev = Device()
    try:
        while True:
            addr, ep, flags, length = HDR.unpack(recv_exact(s, HDR.size))
            payload = b""
            if not (ep & DIR_IN) and length > 0:
                payload = recv_exact(s, length)
            ret, data = dev.handle(ep, flags, length, payload)
            s.sendall(HDR.pack(dev.addr, ep, flags, ret))
            if (ep & DIR_IN) and ret > 0:
                s.sendall(data)
    except (ConnectionError, OSError) as e:
        print(f"[dev] link down: {e}", flush=True)
    finally:
        s.close()


if __name__ == "__main__":
    main()
