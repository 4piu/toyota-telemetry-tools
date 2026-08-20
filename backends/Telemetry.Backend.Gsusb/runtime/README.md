# Bundled runtime dependency

`libusb-1.0.dll` (1.0.30, x64) — the [libusb](https://libusb.info/) project,
licensed [LGPL-2.1](https://github.com/libusb/libusb/blob/master/COPYING).
Sourced from the [`libusb-package`](https://pypi.org/project/libusb-package/)
PyPI distribution and verified working against real gs_usb hardware in this
project's development history before being committed here.

Used only by `LibUsbTransport`, the fallback path — see
[`IGsUsbTransport`](../IGsUsbTransport.cs) for why a fallback exists at all.
The primary transport, `WinUsbTransport`, calls `winusb.dll`, which Windows
ships; it needs nothing from this directory.

`build.ps1` copies this file next to `telemetry-gsusb.exe`. LGPL-2.1 permits
redistributing the unmodified shared library alongside a proprietary or
differently-licensed application, which is what this is: a plain P/Invoke
dependency, not a statically linked or modified component.
