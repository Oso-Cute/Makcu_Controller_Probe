# Manual flashing (fallback)

Use this only if `Flash_Probe_Firmware.bat` fails. It does the same thing
by hand with plain `esptool` commands.

Both images are complete merged binaries written at flash offset `0x0`:

```text
Left\MERGED_left.bin    → Left MCU  (USB1, left port)
Right\MERGED_right.bin  → Right MCU (USB3, right port)
```

Do not swap them.

## 0. Install esptool

```powershell
python -m pip install esptool
```

## 1. Flash the LEFT MCU

1. Disconnect every cable from the board.
2. Hold the **Left** MCU's BOOT button, then plug **USB1 (left port)** into
   this PC while still holding it. Release after the cable is in.
3. A new COM port appears (Device Manager → Ports; the Left MCU in download
   mode shows USB `VID_303A&PID_0009`). Note the COM number.
4. Run (replace `COM7` with your port):

```powershell
python -m esptool --chip esp32s3 --port COM7 --baud 921600 write_flash 0x0 Left\MERGED_left.bin
```

5. Unplug USB1.

A final message like "can not exit download mode" or an Error 1 **after the
write finishes** is normal — the write already succeeded.

## 2. Flash the RIGHT MCU

1. Plug **USB3 (right port)** into this PC. No button is needed; the Right
   MCU exposes a flashable native USB port (`VID_303A&PID_1001`).
2. Run (replace `COM8` with your port):

```powershell
python -m esptool --chip esp32s3 --port COM8 --baud 921600 write_flash 0x0 Right\MERGED_right.bin
```

3. Unplug USB3.

## 3. Reconnect for the probe run

Disconnect everything for ten seconds, then follow the cabling steps in
`README_FIRST.md` and run `Run_Controller_Probe.bat`.
