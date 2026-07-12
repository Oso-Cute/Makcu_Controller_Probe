# MAKCU Controller Probe

The Controller Probe records the USB identity, descriptors, endpoints, Xbox
enumeration, handshake packets, and controller input reports needed to add or
diagnose controller support.

It does **not** automatically make an unsupported controller work. It produces
the evidence needed to make the correct firmware change without guessing.

## What the recipient gets

After one guided run, the tool creates two ZIP files:

- `EMAIL_TO_DEVELOPER_....zip` — full raw trace, exact descriptors, packet
  bytes, and a readable report. Email this privately to the firmware developer.
- `PUBLIC_REDACTED_....zip` — controller serial, endpoint packet bytes,
  control payloads, PC name, and COM port removed. This is safer to post.

The full bundle can contain controller serial or authentication traffic. Do
not post it publicly.

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
5. Follow the prompts exactly. The middle USB connection goes to the PC,
   controller goes to USB3, and Xbox USB1 is connected last when prompted.
6. Perform each requested stick/button action.
7. Find the finished ZIPs under `Controller_Probe_Reports`.
8. Email the `EMAIL_TO_DEVELOPER_...zip` privately.
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
- Xbox control-transfer setup, completion status, and bounded data samples
- Physical pre-mount startup packets
- Change-only post-mount input samples, preventing 1 kHz log flooding
- Xbox-to-controller endpoint OUT packets and completion status
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
