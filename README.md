# usbmuxd with a QEMU backend

A fork of [libimobiledevice/usbmuxd](https://github.com/libimobiledevice/usbmuxd)
whose USB backend talks to a QEMU-emulated iPod touch 2G over TCP instead of to
libusb. The mux protocol itself is untouched upstream code — only the transport
is replaced.

- `usbmuxd/src/usb-qemu.c` — the backend, built in place of `usb.c`.
- `fake_device.py` — a fake iPod touch that speaks the same wire protocol, so
  the backend can be tested without a five-minute emulator boot.

## The transport

QEMU's `hw/arm/ipod_touch_tcp_usb.c` is the TCP **client** and dials us, so the
backend listens. Every exchange is a 5-byte packed header

```c
struct { uint8_t addr; uint8_t ep; uint8_t flags; int16_t length; };
```

followed by a payload for OUT requests and IN responses. `flags` is
`setup=1 | reset=2 | enumdone=4`. A negative returned length is a QEMU
`USB_RET_*` code — `-2` NAK, `-3` STALL — and NAK means *retry*: every endpoint
NAKs until the guest arms it, which is the entire flow-control mechanism. The
device overwrites `addr` in its reply with its own DCFG address, which is how
the host learns `SET_ADDRESS` took effect.

The device model is transfer-oriented and has no concept of a control transfer:
SETUP, data and status are three independent endpoint transactions, and iOS's
own USB stack supplies the descriptors. So `usb-qemu.c` does host-side
enumeration by hand — reset, enumdone, `GET_DESCRIPTOR(DEVICE)`, `SET_ADDRESS`,
the configuration tree, `SET_CONFIGURATION`, string descriptors — then looks for
the interface at class 255 / subclass 254 / protocol 2 and pumps its bulk
endpoints.

Because the device never speaks unprompted, no file descriptor ever becomes
readable to announce incoming data; IN transfers only happen when the host
polls. `usb_get_timeout()` therefore returns a few milliseconds while a device
is attached, and `main_loop()` was patched to give the backend a turn even on a
client-only wakeup.

## Building (macOS, Homebrew)

```sh
brew install libplist libimobiledevice-glue libimobiledevice libusb automake autoconf libtool pkg-config
cd usbmuxd
./autogen.sh --prefix=$PWD/../prefix --without-preflight --without-systemd
make
```

`--without-preflight` skips the lockdownd handshake on device attach. iOS 2.x
lockdownd uses 2008-era TLS that OpenSSL 3 refuses, so preflight would only
produce noise.

## Running

Never let this bind `/var/run/usbmuxd` — Apple's own daemon owns that.

```sh
USBMUXD_QEMU_ADDR=127.0.0.1:1235 \
  ./usbmuxd/src/usbmuxd -f -v -v -S 127.0.0.1:27015 -P NONE -C ./run/conf
```

Clients are pointed at it with `USBMUXD_SOCKET_ADDRESS`:

```sh
USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015 idevice_id -l
```

### Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `USBMUXD_QEMU_ADDR` | `127.0.0.1:1235` | Address to listen on for QEMU. |
| `USBMUXD_QEMU_DELAY` | `0` | Seconds to wait after QEMU connects before driving USB reset. The guest needs roughly 100 s of boot before it has programmed the OTG core. |
| `USBMUXD_QEMU_POLL_MS` | `3` | Bulk IN poll interval while a device is attached. |

## Testing without the emulator

```sh
./usbmuxd/src/usbmuxd -f -v -v -S 127.0.0.1:27015 -P NONE -C ./run/conf &
python3 fake_device.py 127.0.0.1 1235 &
USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015 idevice_id -l
```

The fake device chunks EP0 IN at 64 bytes and NAKs when it has nothing queued,
so the host's retry and reassembly paths get exercised.

## Against the real emulator

```sh
USBMUXD_QEMU_ADDR=127.0.0.1:1237 USBMUXD_QEMU_DELAY=100 \
  ./usbmuxd/src/usbmuxd -f -v -v -S 127.0.0.1:27015 -P NONE -C ./run/conf &

export IT_PMU_FORCE="04=08" IT_USB_GATE2=1 IT_USB_TCP="127.0.0.1:1237"
qemu-system-arm -M iPod-Touch,bootrom=…/bootrom_240_4,nand=…/nand,nor=…/nor_n72ap.bin \
  -serial file:./run/s.log -cpu max -m 2G -display none
```
