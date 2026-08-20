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
2. Flash **both** probe images with the **MAKCU AIO tool**:
   <https://github.com/terrafirma2021/MAKCU_AIO_PUBLIC>
   - `firmware/MERGED_left.bin`  → Left MCU  (USB1, left port)
   - `firmware/MERGED_right.bin` → Right MCU (USB3, right port)

   Do not swap them. Put a side into download mode first (hold its BOOT button
   while plugging its USB), then in the AIO tool switch to **Offline** and use
   **Flash Left / Flash Right** to pick the matching `.bin`.
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

   In both tests, if the controller does not light up within a few seconds of
   being connected, press its power (Xbox/home) button once.

   Use an Xbox for an Xbox/GIP handshake diagnosis. A PC produces a useful
   comparison capture but cannot prove the Xbox handshake.
6. Perform each requested stick/button action.
7. Find the finished ZIP under `Controller_Probe_Reports` — the session
   folder contains only the ZIP.
8. Send the `SEND_TO_OSO_CUTE_...zip` file privately to oso_cute.
9. Restore your normal gameplay firmware afterward with the same AIO tool if
   desired.

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
```

The merged `firmware/MERGED_left.bin` and `firmware/MERGED_right.bin` are the
shippable probe images. Flash them with the MAKCU AIO tool linked above.

Run parser/report tests:

```powershell
python -m unittest discover -s tools/controller_probe/tests -v
```
