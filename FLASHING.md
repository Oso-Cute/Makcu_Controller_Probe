# Flashing the Tested MAKCM Pair

The July 12 Xbox recovery adds shared IPC frame types and changes the matched
inter-MCU UART rate to 2 Mbps. **Flash both Left and Right.** An old side and a
new side cannot communicate correctly.

## Recommended method

Double-click:

```text
firmware\Flash_MAKCM.bat
```

The BAT finds Python, installs/checks `pyserial` and `esptool`, and opens the
guided flasher. The flasher uses:

```text
firmware\Left\MERGED_left.bin
firmware\Right\MERGED_right.bin
```

Both are complete 4 MB-layout images written at offset `0x0`; they are not
application-only PlatformIO `firmware.bin` files.

## USB identities

The wizard detects hardware by USB identity, not fixed COM number:

| Device/mode | VID:PID seen in testing |
|---|---|
| Left ESP32-S3 download mode | `303A:0009` |
| Right ESP32-S3 download mode | `303A:1001` |
| Middle CH343 accessibility port | `1A86:55D3` |

## Guided sequence

1. Connect only USB2/middle to the PC and confirm the CH343 is detected.
2. Put Left/USB1 in download mode by holding its flash button while connecting
   USB1 to the PC.
3. Flash Left and wait for `Hash of data verified`.
4. Disconnect USB1.
5. Connect Right/USB3 to the PC in its flashable native-USB mode.
6. Flash Right and wait for `Hash of data verified`.
7. Disconnect everything for about ten seconds.
8. Reconnect USB3 to the XIM/controller.
9. Reconnect USB2/middle to the PC.
10. Reconnect USB1/Left to Xbox last.

## Normal Left reset warning

After a verified Left write, esptool may report that an ESP32-S3 entered with
GPIO0 cannot be reset automatically and may return `Error 1`. If every region
says `Hash of data verified`, the write succeeded. The full disconnect/reconnect
cycle exits download mode and runs the application.

## Manual merged-image commands

Use these only if the guided flasher cannot run. Replace the COM ports with the
ones shown on your computer.

```powershell
python -m esptool --chip esp32s3 --port COM3 --baud 921600 write-flash 0x0 firmware\Left\MERGED_left.bin
python -m esptool --chip esp32s3 --port COM4 --baud 921600 write-flash 0x0 firmware\Right\MERGED_right.bin
```

Never flash the Left image to Right or the Right image to Left.

## Verify image integrity

```powershell
Get-FileHash -Algorithm SHA256 firmware\Left\MERGED_left.bin
Get-FileHash -Algorithm SHA256 firmware\Right\MERGED_right.bin
```

Expected values are in `firmware/SHA256SUMS.txt`.

## Accessibility check after flashing

1. Ensure USB2/middle is connected to the PC.
2. Run `accessibility\launch_makcu_gui.bat`.
3. Select the CH343 port if it is not already COM5.
4. Connect and expect a response beginning with `kmbox:`.
5. Test one short button tap.
6. Use **Release all** before closing.
