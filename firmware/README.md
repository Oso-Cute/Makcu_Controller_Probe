# MAKCM Xbox Passthrough — Tested Binary Pair

This folder contains the complete Left and Right ESP32-S3 images from the
Xbox Series X passthrough build validated on July 12, 2026.

## Layout

```text
firmware/
├── Left/
│   └── MERGED_left.bin
├── Right/
│   └── MERGED_right.bin
├── Flash_MAKCM.bat
├── flash_tool.py
├── SHA256SUMS.txt
├── MAKCM_ESP32s3_Pass_Left_IDF/   source project (kept for reference)
└── MAKCM_ESP32s3_Pass_Right/      source project (kept for reference)
```

Both `MERGED_*.bin` files are complete images intended to be written at flash
offset `0x0`. Each contains its own bootloader, partition table, boot/OTA data,
and application. Do not substitute one side's image for the other.

## Validated configuration

- Hardware: dual-MCU MAKCM ESP32-S3 board layout.
- Target: Xbox Series X.
- Physical device observed as `VID_045E/PID_0B12`.
- Left/Right UART1 IPC: matched 2,000,000 baud.
- Accessibility UART0: CH343 at 4,000,000 baud.
- Stale control timeout: independent five-second watchdog.
- Xbox Microsoft OS `0xEE` descriptor: mirrored from the physical device.
- GIP startup: real `0x02` announce cached and replayed after target mount.
- Final validation: Xbox produced EP2 OUT traffic and controller movement
  worked through the full passthrough.

These images are the diagnostic build used for the successful test. Left has
COM-port diagnostics enabled, which is useful while validating accessibility
inputs. A later quiet release can suppress those logs without removing the
functional fixes.

## Flashing

Run:

```text
Flash_MAKCM.bat
```

The BAT installs/checks `pyserial` and `esptool`, then opens the guided flasher.
The flasher now loads the two local images shown above; it no longer references
the old `macku_helios-cont` directory.

The wizard identifies ports by USB VID/PID:

- Left download mode: Espressif `VID_303A/PID_0009`.
- Right download mode: Espressif `VID_303A/PID_1001`.
- Middle accessibility/debug port: CH343 `VID_1A86/PID_55D3`.

COM numbers can change between computers. The guided tool uses the USB IDs
rather than relying on COM3/COM4/COM5 assignments.

### After both flashes

1. Disconnect all three USB connections for about ten seconds.
2. Connect USB3 to the XIM/controller.
3. Connect USB2/middle to the PC.
4. Connect USB1/Left to Xbox last.

An ESP32-S3 message saying it cannot automatically exit GPIO0 download mode is
normal after a verified Left flash; the full power cycle starts the firmware.

## Accessibility test

From the repository root, run:

```text
accessibility\launch_makcu_gui.bat
```

The current local `accessibility/config.json` uses COM5. Change it if Windows
assigns the CH343 another port. The GUI and `makcu_access.py` scan through
diagnostic output to find the `kmbox:` handshake, so the tested diagnostic
firmware can remain installed.

Supported test controls include:

- A/B/X/Y;
- LB/RB;
- LT/RT;
- right-stick delta movement;
- tremor smoothing, deadzone, and trim;
- live physical-controller telemetry.

Always use **Release all** after testing latched buttons. The firmware also has
a stale-release safety path for injected stick motion.

## Compatibility scope

The binaries are not tied to one controller serial number. Descriptors and
strings are obtained from the connected physical device at runtime.

They are currently tested only with the MAKCM hardware and the downstream
device presenting `045E:0B12`. Other units presenting the same identity and GIP
protocol are likely compatible. Different VID/PID devices are experimental:
the Xbox-specific qualifier and Microsoft `0xEE` handling is deliberately
gated to `045E:0B12`.

Left and Right are a matched protocol pair. Never flash only one of these over
an older build, and never mix a 2 Mbps side with a 5 Mbps side.

## Integrity

Verify the images before sharing or flashing:

```powershell
Get-FileHash -Algorithm SHA256 Left\MERGED_left.bin
Get-FileHash -Algorithm SHA256 Right\MERGED_right.bin
```

Expected values are stored in `SHA256SUMS.txt`.
