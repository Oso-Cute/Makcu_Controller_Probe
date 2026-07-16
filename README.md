# MAKCU Controller Probe

The Controller Probe records the USB identity, descriptors, endpoints, host
enumeration, handshake packets, and controller input reports needed to add or
diagnose controller support.

It does **not** automatically make an unsupported controller work. It produces
the evidence needed to make the correct firmware change without guessing.

## Why this exists

The primary goal is to create a private, reproducible evidence package that a
developer can use to support a controller they do **not** physically own. A
useful capture records the exact probe-image pair, physical connection and
activation order, USB descriptors, startup packets, host control traffic,
endpoint OUT handshake, and real input reports. Firmware experiments may
generate hypotheses, but the raw probe report—not an anecdotal “it worked once”
result—is what another developer should be able to use to build compatible
bins.

## What the recipient gets

After one guided run, the tool creates one ZIP file:

- `SEND_TO_OSO_CUTE_....zip` — full raw trace, exact descriptors, packet
  bytes, and a readable report. Send this ZIP to oso_cute.

The tool opens the report folder when it finishes. The ZIP can contain
controller serial or authentication traffic, so do not post it publicly.

## Requirements

- Windows 10/11
- Python 3.10 or newer
- MAKCU/MAKCM board
- Both `LEFT_PROBE` and `RIGHT_PROBE` images flashed
- Data-capable USB cables
- Controller connected directly by USB for the first compatibility test

## Recipient workflow

1. Back up the currently working Left and Right firmware.
2. Run `Flash_Probe_Firmware.bat` and flash **both** probe images.
3. Disconnect every cable for ten seconds.
4. Double-click `Run_Controller_Probe.bat`.
5. Answer the controller questions first. The tool then records both tests in
   one session:
   - **Cold start:** USB2 → PC and USB3 → controller are connected before the
     tool arms the capture. USB1 → console or main PC is connected last; it
     powers Left and makes the CH343 COM port usable. After the countdown, the
     tool detects the port automatically and starts the probe checks without
     another Enter key.
   - **Controller hot-plug:** USB2 → PC and USB1 → target host stay connected.
     The tool records the controller being unplugged and then reconnected to
     USB3.

   Use an Xbox for an Xbox/GIP handshake diagnosis. A PC produces a useful
   comparison capture but cannot prove the Xbox handshake.
6. Perform each requested stick/button action.
7. Find the finished ZIP under `Controller_Probe_Reports`.
8. Send the `SEND_TO_OSO_CUTE_...zip` file privately to oso_cute.
9. Restore normal gameplay firmware after the capture if desired.

## Command-line use

```powershell
python controller_probe.py --port COM5
```

Useful options:

```text
--quick             Shorter waits for development
--no-actions        Skip guided input-report mapping
--allow-legacy      Continue without both probe firmware banners
--analyze FILE      Rebuild reports from an existing raw log
--output DIRECTORY  Choose where report folders are written
```

Offline analysis does not require pyserial:

```powershell
python controller_probe.py --analyze raw_serial.log --controller-name "GameSir G7 Pro"
```

## Information captured

- VID, PID, USB/bcdDevice versions, speed, EP0 size, and string indices
- Exact device, configuration, and available string descriptors
- Every interface, alternate setting, and endpoint declaration
- Microsoft OS `0xEE`, qualifier, and other-speed probe results
- Host control-transfer setup, completion status, and bounded data samples
- Physical pre-mount startup packets
- Change-only post-mount input samples, preventing 1 kHz log flooding
- Host-to-controller endpoint OUT packets and completion status
- Mount/reset/announce-cache/replay state
- IPC CRC failures and stale-control recoveries

## Developer build

From the repository root:

```powershell
pio run -d firmware/MAKCM_ESP32s3_Pass_Left_IDF -e LEFT_PROBE
pio run -d firmware/MAKCM_ESP32s3_Pass_Right -e RIGHT_PROBE
powershell -ExecutionPolicy Bypass -File tools/controller_probe/build_package.ps1 -SkipBuild
```

Run parser/report tests:

```powershell
python -m unittest discover -s tools/controller_probe/tests -v
```
