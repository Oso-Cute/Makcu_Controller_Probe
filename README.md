# MAKCU/MAKCM Xbox Passthrough + Accessibility

This is the working dual-ESP32-S3 MAKCM package validated on Xbox Series X on
July 12, 2026.

The board passes a physical XIM/controller through to Xbox and exposes an
accessibility command channel through the middle CH343 USB port. The validated
firmware fixed Xbox enumeration, stale control recovery, and GIP startup
ordering. Physical controller movement and injected A-button inputs were both
confirmed through the complete path.

## Layout

```text
macku_controller/
├── README.md
├── FLASHING.md
├── accessibility/
│   ├── launch_makcu_gui.bat
│   ├── makcu_access.py
│   ├── makcu_gui.py
│   ├── makcu_monitor.py
│   ├── config.json
│   └── README.md
├── firmware/
│   ├── Flash_MAKCM.bat
│   ├── flash_tool.py
│   ├── Left/MERGED_left.bin
│   ├── Right/MERGED_right.bin
│   ├── SHA256SUMS.txt
│   └── README.md
└── _FO_Docs/
```

The firmware source projects remain under `firmware/` for reference, but they
are not required when using the merged images and guided flasher.

## Quick start

### Flash both MCUs

Run:

```text
firmware\Flash_MAKCM.bat
```

Follow the wizard. The two images are a matched 2 Mbps protocol pair; both
sides must be flashed.

### Reconnect normally

After flashing:

1. Disconnect all three USB connections for about ten seconds.
2. Connect USB3 to the XIM/controller.
3. Connect USB2/middle to the PC.
4. Connect USB1/Left to Xbox last.

### Test accessibility

Run:

```text
accessibility\launch_makcu_gui.bat
```

The local configuration currently uses COM5. If Windows changes the CH343 COM
number, select the detected CH343 port in the GUI or edit
`accessibility/config.json`.

Available assistance includes:

- latch/toggle for A, B, X, Y, LB, RB, LT, and RT;
- right-stick delta movement;
- right-stick tremor smoothing and deadzone;
- drift trim;
- physical-controller telemetry;
- explicit **Release all** safety control.

## Validated result

- Xbox enumerated the mirrored `045E:0B12` controller.
- Microsoft OS `0xEE` and vendor-control traffic completed.
- Xbox sent real EP2 OUT handshake/authentication packets.
- IPC CRC failures in the final validation capture: zero.
- Physical controller movement worked.
- `km.version()` responded on COM5.
- Nine commanded A-button taps were transmitted with explicit releases.

## Compatibility

The binaries are not tied to the tested controller's serial number. They read
physical descriptors at runtime. They are currently validated only for the
MAKCM board and a downstream device presenting `VID_045E/PID_0B12` with GIP
behavior. Other hardware or controller identities are experimental.

See `firmware/README.md` for image checksums and `FLASHING.md` for detailed
flashing instructions.
