# MAKCU/MAKCM Xbox Series X Passthrough Recovery

**Investigation date:** July 12, 2026 (America/Los_Angeles)  
**Final status:** Successful on Xbox Series X  
**User validation:** Controller movement reaches the Xbox through the complete passthrough chain  
**Working source tree:** `C:\DevTools\RENG\PROJ_3\makcu_stable`  
**Detailed source summary:** `C:\DevTools\RENG\PROJ_3\makcu_stable\PATCH_SUMMARY.md`

---

## 1. Executive summary

The passthrough is working. The final proof was not merely that the mirrored
controller enumerated: the Xbox sent real interrupt-OUT traffic through Left,
across IPC, and toward the physical XIM/controller, and controller movement
then worked on the Xbox.

Three separate issues were found during the investigation:

1. **A real stale-control-state bug existed on Left.** If a
   `FRAME_CTRL_STATUS` IPC frame was lost, the original `ctl_pending` timeout
   was checked only when a new SETUP arrived. The device could therefore stay
   wedged if the host stopped retrying. This was fixed with an independent
   five-second watchdog, atomic state handling, and safe late-frame rejection.
   This was a confirmed code weakness, but it was not the final Xbox-specific
   blocker observed during the successful test.

2. **Xbox enumeration required the physical device's Microsoft OS 1.0 string
   descriptor at index `0xEE`.** Once Left mirrored the exact `MSFT100`
   descriptor and continued forwarding the associated vendor requests, Xbox
   reached `SET_CONFIGURATION`, mounted the custom passthrough driver, and
   began polling EP82. Descriptor mirroring fixed enumeration, but input still
   did not work at that stage.

3. **The functional blocker was GIP handshake ordering.** Right saw the
   physical device's initial GIP announce (`command 0x02`) before Left was
   mounted. Right's old synthetic `gip_kickstart()` answered that announce
   locally with identify, power, and LED commands. The physical device moved
   into streaming mode before Xbox could see the announce. Xbox therefore
   never initiated its own interrupt-OUT handshake. Right was changed to cache
   the real announce, stop generating GIP commands, hold IN traffic until Left
   is mounted, and replay the exact announce to Xbox after mount. That produced
   Xbox EP2 OUT traffic and working movement.

The original 65-byte HID-report theory was deliberately not pursued as a
blanket buffer-size change. Windows HID APIs commonly expose a 65-byte user
buffer containing a report-ID byte while the actual USB transaction remains a
64-byte endpoint payload. No endpoint packet size or 64-byte USB data buffer
was blindly changed.

---

## 2. Final verified result

| Check | Before final GIP fix | After final GIP fix |
|---|---:|---:|
| Xbox enumeration reaches `SET_CONFIGURATION` | Yes, after `0xEE` mirror | Yes |
| EP82 IN activity visible | Yes | Yes |
| Xbox-to-device EP2 OUT packets in a 25-second capture | **0** | **54** |
| IPC CRC failures in final 25-second capture | Not the observed blocker | **0** |
| Stale-control timeout events in final capture | Not observed | **0** |
| Physical movement reaches Xbox | No | **Yes** |

The final filtered COM5 capture ran for 25 seconds at 4,000,000 baud and
recorded:

```text
duration_s=25
serial_bytes=49150
[L][EP] OUT=54
CRC_FAIL=0
STALE_=0
```

Representative Xbox OUT packets included:

```text
[L][EP] OUT ... ep=02 len=13 hex=01200c09000620060000000000
[L][EP] OUT ... ep=02 len=18 hex=0630040e0042002200b00000000000000000
[L][EP] OUT ... ep=02 len=64 hex=06f007ba005200410025004425020040fcb48a2f...
[L][EP] OUT ... ep=02 len=5  hex=1e30050100
[L][EP] OUT ... ep=02 len=9  hex=0d200605000c824b46
```

These packets are decisive because they are received by Left's real USB OUT
endpoint callback and forwarded as `FRAME_EP_OUT`. Before the announce-replay
fix, the same stage produced no OUT traffic at all.

---

## 3. Confirmed hardware and firmware architecture

The functional data path is:

```text
Xbox Series X / PC
        |
        | USB (Left presents the mirrored device)
        v
Left ESP32-S3 MCU
TinyUSB device stack
        |
        | UART1 IPC, current default 2,000,000 baud
        | framed packets + CRC16-CCITT
        v
Right ESP32-S3 MCU
ESP-IDF USB Host stack
        |
        | USB host connection
        v
Physical XIM / controller device
```

The separate diagnostic/control path is:

```text
Left UART0 -> CH343 USB serial bridge -> PC COM5 at 4,000,000 baud
```

The three normal USB connections used in the successful cold-start test were:

1. **USB3:** Right-side physical XIM/controller connection.
2. **USB2 / middle:** PC diagnostic connection, exposing CH343 COM5.
3. **USB1:** Left-side connection to Xbox, connected last.

The final cold-start order was USB3, then USB2/middle, then USB1/Xbox. The
firmware still waits for Right's descriptor snapshot before exposing Left, so
this order is helpful but is not a substitute for the firmware state machine.

---

## 4. Initial symptom and investigation boundaries

The initial symptom was that passthrough would enumerate inconsistently or
stop responding, especially around host control traffic. The first requested
focus was a possible stale `ctl_pending` state caused by a dropped
`FRAME_CTRL_STATUS` at the 5 Mbps inter-MCU UART rate.

Important constraints followed throughout the work:

- Do not replace every 64-byte buffer with 65 bytes.
- Do not change endpoint maximum packet sizes without wire-level evidence.
- Do not rewrite the passthrough stack.
- Do not change descriptor mirroring unless a host trace proves it is needed.
- Keep recovery changes small, reversible, and observable through targeted
  logs.
- Keep Left and Right IPC frame definitions and baud rates exactly matched.

Those constraints were correct. The final working patch did not alter endpoint
packet sizes or USB payload buffer widths.

---

## 5. Why the 65-byte HID API theory was not the main fix

`Flash.exe` uses 65-byte buffers with the Windows HID API. That does not by
itself prove that the USB endpoint transfers 65 bytes. A typical Windows HID
write buffer is:

```text
byte 0      report ID used by the HID API
bytes 1-64  actual report payload
```

For a device whose USB endpoint maximum packet size is 64, Windows can consume
the report-ID byte in the HID layer and place a 64-byte OUT transaction on the
wire. Therefore, converting every Left endpoint buffer from 64 to 65 would
have mixed an API representation with a USB transaction representation. It
could also have broken TinyUSB endpoint logic or introduced off-by-one errors.

What was done instead:

- Existing 64-byte endpoint buffers were retained.
- Actual control, enumeration, IN, and OUT behavior was instrumented.
- The Xbox-specific failure was located through the sequence of real USB and
  IPC events.

This remains the right direction unless a future USB capture proves an actual
65-byte bus transaction for a specific endpoint.

---

## 6. Stale `ctl_pending` analysis

### 6.1 Original failure mode

The original lifecycle allowed this sequence:

1. Host sends a control SETUP.
2. Left sets `ctl_pending = true` and sends `FRAME_CTRL_SETUP` to Right.
3. Right completes the physical control transfer and sends
   `FRAME_CTRL_STATUS` back.
4. UART corruption or a lost byte causes the CRC check to reject that frame.
5. Left never sees the status and leaves `ctl_pending` true.
6. A following SETUP sees BUSY and returns `false`, causing EP0 STALL behavior.
7. If the host stops sending SETUPs after repeated failures, the timeout check
   is never executed again.

The weakness was therefore confirmed by code inspection: the old timeout was
two seconds but traffic-dependent. It was possible for a dropped status to
leave the device wedged indefinitely despite the apparent timeout constant,
because no code outside the next SETUP path performed the expiry check.

### 6.2 Current `ctl_pending` lifecycle

Line numbers below refer to the successful July 12 source snapshot.

| Operation | Function/location | Current behavior |
|---|---|---|
| Timeout constant | Left `pass_usb_device.c:204-205` | `CTL_PENDING_TIMEOUT_MS = 5000` |
| State declaration | `pass_usb_device.c:209-213` | Sequence, pending flag, request copy, and timestamp |
| Initialization clear | `pass_driver_init()`, around `232` | Clears pending state under `ctl_lock` |
| USB bus-reset clear | `pass_driver_reset()`, around `252` | Clears pending state under `ctl_lock` |
| Existing-pending check | `pass_driver_control_xfer()`, `342-353` | Atomically distinguishes BUSY from stale |
| Set true | `pass_driver_control_xfer()`, `355-359` | Copies request, increments sequence, stores timestamp |
| IPC send failure clear | `392-395`, `416-419`, `452-455` | Does not leave state pending when forwarding fails |
| ACK clear | `464-477` | Clears after TinyUSB ACK stage |
| Independent timeout clear | `pass_usb_control_watchdog()`, `487-513` | Runs without requiring another SETUP |
| Disconnect clear | `pass_usb_disconnect()`, `806-821` | Clears and logs before dropping D+ |
| IN data sequence check | `pass_usb_control_in_complete()`, `835-860` | Ignores late/nonmatching data safely |
| Status sequence check and clear | `pass_usb_control_status()`, `863-897` | Clears only a matching active sequence |

### 6.3 Independent watchdog

`pass_usb_control_watchdog()` is called from Left's ongoing LED/state task in
`main.c:445-447`. It is therefore independent of host SETUP traffic. When the
host is visible, that task normally runs on a roughly 100 ms loop; in earlier
pipeline states the loop is slower, but it continues to run.

When a pending transfer reaches five seconds, the watchdog emits:

```text
[L][CTL] STALE_TIMEOUT_CLEAR seq=N age_ms=... timeout_ms=5000 pending=0
```

The SETUP path also retains a fast stale check as a second layer:

```text
[L][CTL] STALE_CLEAR seq=N age_ms=... source=setup pending=0
```

### 6.4 Safe late status behavior

If Right's status arrives after the watchdog has cleared the state, it still
carries its original sequence. Left computes:

```c
match = ctl_pending && seq == ctl_pending_seq;
```

It then logs and ignores a nonmatching status instead of touching an unrelated
new request:

```text
[L][CTL] STATUS_RX seq=N expected=M ... match=0 ignored=1 pending=...
```

The same sequence discipline is applied to late `FRAME_CTRL_IN_DATA` payloads.

### 6.5 Concurrency protection

TinyUSB callbacks, IPC RX, and the watchdog execute in different tasks. The
patch protects the small control-state snapshots with `ctl_lock`, a FreeRTOS
port critical-section lock. USB and UART functions are deliberately called
after releasing the lock. This avoids racing a timeout against a status
completion and avoids blocking while holding a low-level critical section.

### 6.6 What the final test says about this theory

The stale-state theory was **confirmed as a valid bug in the original code**,
but it was **not observed as the final Xbox blocker**. The final working
25-second run produced zero `CRC_FAIL` and zero stale timeout logs. The patch
should still be kept because it closes a real permanent-wedge path.

---

## 7. IPC reliability changes

### 7.1 Matched baud constant

Both projects now define the same guarded constant in their respective copies
of `include/pass_ipc.h`:

```c
#ifndef PASS_IPC_UART_BAUD
#define PASS_IPC_UART_BAUD 2000000UL
#endif
```

The original comparison rate remains available at build time:

```text
-DPASS_IPC_UART_BAUD=5000000UL
```

Both sides must always be built and flashed at the same rate. Mixing a 2 Mbps
Left with a 5 Mbps Right makes IPC unusable.

### 7.2 CRC diagnostics

Both deframers now retain:

- CRC failure count;
- received frame type;
- payload length;
- sequence number;
- expected and received CRC;
- time since the last valid frame.

Logging is throttled to the first four failures and then every 100th failure.
This gives useful evidence without making UART timing significantly worse.

Example Left log:

```text
[L][IPC] CRC_FAIL count=1 type=0x21 len=1 seq=42
expected=0x.... got=0x.... last_good_age_ms=...
```

Right uses the same information and tunnels its diagnostic message back to
Left as `FRAME_LOG`.

### 7.3 Frame-transmit serialization

IPC frames are assembled completely before transmission and protected by a TX
mutex. This matters because USB completion callbacks, IPC RX responses, and
other callbacks can transmit from multiple tasks/cores. The mutex prevents
separate frames from interleaving at the byte level.

### 7.4 What is and is not known about 5 Mbps

The final successful firmware used the 2 Mbps default and showed zero CRC
failures in the validation capture. That proves 2 Mbps was stable for this
test. It does **not** prove that 5 Mbps is always unstable; a controlled final
5 Mbps A/B run has not yet been completed. The 5 Mbps option remains for a
future comparison after the working 2 Mbps state is preserved.

---

## 8. Xbox enumeration investigation

### 8.1 Windows was used as a control test

Connecting Left to Windows proved that the basic mirrored USB device and IN
pipeline were functional. Windows reported:

- PnP status `OK`;
- class `XboxComposite`;
- name `Xbox Controller`;
- VID/PID `045E:0B12`;
- successful `SET_CONFIGURATION`;
- `tud_mount_cb`;
- continuing EP82 IN activity.

This narrowed the problem from “USB passthrough is generally broken” to an
Xbox-specific enumeration/handshake requirement.

### 8.2 TinyUSB level-2 trace

The Left diagnostic build temporarily enables TinyUSB device debug level 2 in
both `sdkconfig.defaults` and the generated `sdkconfig.LEFT_IDF`. TinyUSB's
debug output is redirected through `pass_tusb_debug_printf()` and the existing
Left UART0 path with the prefix:

```text
[L][USB]
```

This exposed the exact point where Xbox stopped during each experiment.

### 8.3 Device Qualifier investigation

Xbox requested `GET_DESCRIPTOR` type `0x06`, the Device Qualifier. Two
experiments were important:

1. A qualifier derived from the mirrored device descriptor was returned. The
   transfer completed, but Xbox still stopped. Therefore “always provide a
   plausible qualifier” was not the answer.
2. Left then pre-probed the physical `045E:0B12` device through Right's normal
   control path. The physical device returned **STALL**, not qualifier data.

The final code mirrors the physical result. Because TinyUSB's generic failed
descriptor path attempted to stall both EP0 OUT and EP0 IN, a narrowly gated
linker wrapper suppresses only the qualifier request's EP0-OUT stall and
allows the EP0-IN stall corresponding to the failed IN data stage. Every other
stall passes through unchanged.

Key implementation locations in Left `main.c`:

- `tud_descriptor_device_qualifier_cb()`: `105-125`;
- `__wrap_dcd_edpt_stall()`: `127-143`;
- linker flag: `-Wl,--wrap=dcd_edpt_stall` in `platformio.ini`.

This made qualifier behavior faithful and prevented EP0 OUT from remaining
wedged, but it did not by itself make Xbox passthrough functional.

### 8.4 Microsoft OS 1.0 string descriptor at index `0xEE`

A cold Xbox trace showed:

```text
GET_DESCRIPTOR(String, index 0xEE)
```

This request is standard USB descriptor traffic. It does not reach the
device-recipient vendor callback used for later Microsoft OS requests.
esp_tinyusb's normal fixed string table did not contain index `0xEE`, so Left
needed to pre-probe and mirror the physical device's exact response.

The physical descriptor was:

```text
12 03 4d 00 53 00 46 00 54 00 31 00 30 00 30 00 90 00
```

Decoded, this is:

```text
"MSFT100" + vendor request code 0x90
```

Left now reserves probe sequence `0xFFFC`, requests the physical descriptor
through Right, caches the exact 18 bytes, and returns it only for the gated
`045E:0B12` mirror. All ordinary string indices still call esp_tinyusb's
original callback.

Key implementation locations in Left `main.c`:

- reserved probe sequences: `90-98`;
- `__wrap_tud_descriptor_string_cb()`: `145-161`;
- probe start after `FRAME_DEVICE_READY`: `337-350`;
- probe data/status handling: `361-418`;
- one-second settle before exposing D+: `491-505`;
- linker flag: `-Wl,--wrap=tud_descriptor_string_cb`.

After receiving `MSFT100`, Xbox sent vendor requests with request code `0x90`:

- `C0 90`, `wIndex=4`, `wLength=16`;
- `C0 90`, `wIndex=4`, `wLength=40` for the compatible-ID data;
- `C1 90`, `wIndex=5`, `wLength=10`, which the physical device stalled and
  Left mirrored as a stall.

Once the `0xEE` string was mirrored, Xbox continued to
`SET_CONFIGURATION`, the custom passthrough driver opened, `tud_mount_cb`
ran, and EP82 IN traffic continued. This was the enumeration fix.

### 8.5 Why enumeration success was not enough

At this point the device looked alive:

- descriptors were accepted;
- Xbox configured the device;
- Left mounted;
- EP82 IN packets continued.

However, a 25-second filtered capture produced approximately 184 KB of serial
diagnostics and **zero** `[L][EP] OUT` lines. Xbox was polling an IN endpoint
but had never started the GIP OUT handshake. This distinction led directly to
the final root cause.

---

## 9. Final root cause: GIP announce consumed before target mount

### 9.1 Fault sequence

The original Right behavior was:

1. Right enumerates the physical XIM/controller before Left has finished
   exposing the mirrored device to Xbox.
2. The physical device sends its initial GIP announce packet, whose first byte
   is `0x02`.
3. Right's `in_xfer_complete()` detects `0x02` and invokes
   `gip_kickstart()`.
4. `gip_kickstart()` locally sends synthetic GIP identify (`0x04`), power
   (`0x05`), and LED (`0x0A`) commands to the physical OUT endpoint.
5. The physical device transitions to normal `0x20` input streaming.
6. Early IN packets forwarded toward Left are rejected by
   `pass_usb_submit_in()` because Left is not mounted or `tud_ready()` is
   false.
7. Xbox later configures Left, but the original `0x02` announce is gone. Xbox
   sees an already-streaming device without the event that starts its own
   handshake.
8. Xbox sends no EP2 OUT packets, so authentication/initialization never
   completes end-to-end.

This exactly matched the pre-fix trace: sustained IN activity, no control
failure, and zero endpoint OUT traffic.

### 9.2 Design principle of the fix

Right should be a passthrough, not an independent GIP host layered in front of
Xbox. Xbox must see the real announce and issue its own commands. Those OUT
commands must cross:

```text
Xbox -> Left EP2 OUT -> FRAME_EP_OUT -> Right -> physical EP2 OUT
```

The final fix therefore removes synthetic GIP generation entirely.

### 9.3 New target-state IPC frames

Both `pass_ipc.h` copies define:

```c
FRAME_TARGET_MOUNTED = 0x13
FRAME_TARGET_RESET   = 0x14
```

Left sends:

- `FRAME_TARGET_MOUNTED` from `tud_mount_cb()`;
- `FRAME_TARGET_RESET` from `tud_umount_cb()`;
- `FRAME_TARGET_RESET` from the custom driver's USB bus-reset callback.

Current Left locations:

- state-send helper and mount/unmount callbacks:
  `pass_usb_device.c:147-163`;
- bus-reset send: `pass_usb_device.c:240`.

Right receives the frames in `main.cpp:44-48` and calls
`pass_host.set_target_mounted()`.

### 9.4 Exact announce caching and replay

Right now stores:

```text
target_mounted_
announce_cached_
announce_ep_
announce_len_
announce_buf_[64]
```

These are declared in `PassUsbHost.h:72-76`.

When a physical IN completion begins with `0x02`, Right caches the exact
endpoint, length, and bytes. It does not generate any response. While
`target_mounted_` is false, normal physical IN traffic is not forwarded to
Left.

When Left reports target mount, Right copies the cached announce under a short
lock, releases the lock, and sends the exact packet as `FRAME_EP_IN`. Relevant
locations:

- new-device state reset: `PassUsbHost.cpp:114-122`;
- target mount/reset handling and replay: `297-320`;
- announce cache and IN gating: `322-372`;
- physical disconnect cleanup: `470-478`.

The cached announce is retained across target-side reset/mount cycles. It is
cleared only when a new physical device is opened or the physical device is
released. This is important because the physical device may not emit another
announce merely because Xbox reset Left.

### 9.5 Concurrency behavior

Right's USB completion callback and IPC receive task can run on different
cores. `flow_lock` protects target state and the cached packet. The code copies
the small packet while locked, then performs logging and IPC transmission
outside the critical section. This keeps the lock bounded and avoids blocking
UART operations inside it.

### 9.6 Expected successful order

On a full cold start, the diagnostic sequence should resemble:

```text
[R] [EP] GIP ANNOUNCE_CACHED ep=82 len=...
[L][EP] TARGET_RESET source=bus_reset sent=1
[L] L tud_mount_cb
[L][EP] TARGET_MOUNTED sent=1
[R] [EP] TARGET_MOUNTED announce_cached=1
[R] [EP] GIP ANNOUNCE_REPLAY ep=82 len=... sent=1
[L][EP] IN ... ep=82 ... hex=02...
[L][EP] OUT ... ep=02 ...
[L][EP] IN ... ep=82 ... hex=20...
```

The final capture began after the early boot lines had already passed, so it
did not retain every line above. It did capture 54 EP2 OUT packets and the
user confirmed movement. Those two facts prove that the replay caused Xbox to
enter and complete the previously missing outbound protocol path.

---

## 10. What worked, what did not, and what remains unproven

| Change or theory | Result | Interpretation |
|---|---|---|
| Change all 64-byte buffers to 65 | **Not done** | No bus evidence supported it; likely harmful |
| Independent five-second `ctl_pending` watchdog | **Kept** | Fixes a confirmed permanent-wedge weakness |
| Late control data/status sequence checks | **Kept** | Makes timeout recovery safe |
| Throttled IPC CRC counters | **Kept** | Gives evidence without log flooding |
| Reduce both IPC sides from 5 Mbps to 2 Mbps | **Working** | Final run had zero CRC failures |
| Assume 5 Mbps is definitely bad | **Not proven** | Requires a future controlled A/B test |
| TinyUSB level-2 trace | **Worked diagnostically** | Located exact Xbox stop points |
| Return a guessed/derived Device Qualifier | **Did not fix Xbox** | Physical behavior had to be measured |
| Pre-probe physical qualifier behavior | **Worked diagnostically** | Proved the real device stalls qualifier |
| Suppress only TinyUSB's qualifier EP0-OUT stall | **Defensive/faithful, but not sufficient alone** | Prevents wrong-direction EP0 wedging |
| Mirror physical Microsoft OS `0xEE` string | **Fixed Xbox enumeration** | Xbox reached configuration and mount |
| Live-forward vendor code `0x90` requests | **Worked** | Xbox obtained compatible-ID behavior |
| Keep Right's synthetic GIP kickstart | **Failed functional passthrough** | Consumed announce before Xbox could see it |
| Cache and replay exact GIP announce after Left mount | **Fixed functionality** | Xbox sent EP2 OUT and movement worked |
| Use legacy `flash_tool.py` image paths | **Did not work** | Tool pointed at missing `macku_helios-cont` images |
| Use PlatformIO per-project upload | **Worked** | Both images flashed with verified hashes |

---

## 11. File-by-file change ledger

### Left firmware

#### `firmware/MAKCM_ESP32s3_Pass_Left_IDF/src/pass_usb_device.c`

- Added buffered `[L][USB]` TinyUSB trace output.
- Changed `ctl_pending` timeout to five seconds.
- Added `ctl_lock` protection around shared control state.
- Preserved the SETUP-path stale guard.
- Added independent `pass_usb_control_watchdog()`.
- Cleared pending state on every local forwarding failure path.
- Added sequence-aware late IN-data and status handling.
- Added `[L][CTL]` state-transition diagnostics.
- Added throttled `[L][EP] IN` heartbeats and detailed `[L][EP] OUT` logs.
- Added target mount/reset notifications for Right.
- Left all 64-byte endpoint buffers and endpoint sizes unchanged.

#### `firmware/MAKCM_ESP32s3_Pass_Left_IDF/src/main.c`

- Calls the control watchdog outside the SETUP path.
- Added reserved descriptor-probe sequences.
- Pre-probes the physical `0xEE` string and qualifier behavior.
- Caches and mirrors the exact Microsoft OS string.
- Mirrors physical qualifier STALL behavior.
- Added the narrowly gated EP0 stall-direction wrapper.
- Waits for probe settlement before exposing Left to Xbox.
- Continues routing ordinary control completion frames to the guarded
  `ctl_pending` functions.

#### `firmware/MAKCM_ESP32s3_Pass_Left_IDF/src/ipc.c`

- Uses `PASS_IPC_UART_BAUD`.
- Added CRC failure count and last-good timing.
- Added throttled CRC diagnostics.
- Retains separate 4 Mbps UART0 diagnostic/KM transport.

#### `firmware/MAKCM_ESP32s3_Pass_Left_IDF/include/pass_ipc.h`

- Default baud is 2 Mbps with a compile-time override.
- Added target mount/reset frame definitions.

#### `firmware/MAKCM_ESP32s3_Pass_Left_IDF/platformio.ini`

- Routes TinyUSB debug output through `pass_tusb_debug_printf`.
- Adds `--wrap=dcd_edpt_stall`.
- Adds `--wrap=tud_descriptor_string_cb`.
- Default checked-in build remains `COM3_LOG=0`; the flashed diagnostic build
  explicitly overrode it to `COM3_LOG=1`.

#### `sdkconfig.defaults` and `sdkconfig.LEFT_IDF`

- TinyUSB debug level is temporarily 2 for the diagnostic build.

### Right firmware

#### `firmware/MAKCM_ESP32s3_Pass_Right/include/PassUsbHost.h`

- Removed the synthetic GIP state and helper declarations.
- Added target-mount API and cached-announce state.

#### `firmware/MAKCM_ESP32s3_Pass_Right/src/PassUsbHost.cpp`

- Removed `gip_send()` and `gip_kickstart()`.
- Resets flow state on physical device open/gone.
- Caches the real `0x02` announce.
- Gates physical IN forwarding until target mount.
- Replays the exact cached announce at mount.
- Added `[EP] TARGET_*`, `ANNOUNCE_CACHED`, and `ANNOUNCE_REPLAY` logs.
- Retained real OUT forwarding and control transfer handling.

#### `firmware/MAKCM_ESP32s3_Pass_Right/src/main.cpp`

- Uses the shared baud constant.
- Logs boot baud.
- Handles `FRAME_TARGET_MOUNTED` and `FRAME_TARGET_RESET`.

#### `firmware/MAKCM_ESP32s3_Pass_Right/src/ipc.cpp`

- Added throttled CRC diagnostics.
- Serializes complete TX frames under a mutex.
- Retains the partial-frame inactivity resynchronization guard.

#### `firmware/MAKCM_ESP32s3_Pass_Right/include/pass_ipc.h`

- Matches Left's 2 Mbps default and target-state frame values.

### Documentation and tools

- `PATCH_SUMMARY.md` contains the build, test, log, and rollback summary.
- `firmware/README.md` documents the recovery behavior and baud default.
- `FLASHING.md` emphasizes flashing both MCUs with matching IPC firmware.
- `flash_tool.py` was not used for the successful final flash because its
  expected `MERGED_left.bin` and `MERGED_right.bin` paths pointed at a
  different/legacy `macku_helios-cont` tree.

The exact missing paths reported by that tool were:

```text
C:\DevTools\RENG\PROJ_3\macku_helios-cont\Left\MERGED_left.bin
C:\DevTools\RENG\PROJ_3\macku_helios-cont\Right\MERGED_right.bin
```

Those warnings described missing legacy build artifacts, not a failed probe of
the connected MCUs. The successful final cycle bypassed both merged-image
lookups and let PlatformIO/esptool write each project's bootloader, partition
table, boot-app data, and application image directly.

---

## 12. Files inspected during the investigation

Primary requested files:

- `firmware/MAKCM_ESP32s3_Pass_Left_IDF/src/pass_usb_device.c`
- `firmware/MAKCM_ESP32s3_Pass_Left_IDF/src/ipc.c`
- `firmware/MAKCM_ESP32s3_Pass_Left_IDF/src/main.c`
- `firmware/MAKCM_ESP32s3_Pass_Right/src/PassUsbHost.cpp`
- `firmware/MAKCM_ESP32s3_Pass_Right/src/main.cpp`

Related/shared files:

- both `include/pass_ipc.h` copies;
- Right `include/PassUsbHost.h`;
- Right `src/ipc.cpp`;
- both PlatformIO configurations;
- Left `sdkconfig.defaults` and `sdkconfig.LEFT_IDF`;
- project flashing scripts and documentation;
- esp_tinyusb/TinyUSB callback behavior relevant to string descriptors,
  qualifier handling, and EP0 stalls.

---

## 13. Build procedure

### 13.1 Current 2 Mbps default, quiet build

Run from `C:\DevTools\RENG\PROJ_3\makcu_stable`:

```powershell
Remove-Item Env:PLATFORMIO_BUILD_FLAGS -ErrorAction SilentlyContinue
Remove-Item Env:PLATFORMIO_BUILD_UNFLAGS -ErrorAction SilentlyContinue

pio run -d firmware/MAKCM_ESP32s3_Pass_Left_IDF -e LEFT_IDF
pio run -d firmware/MAKCM_ESP32s3_Pass_Right -e RIGHT
```

The checked-in Left configuration uses `COM3_LOG=0`, so this is appropriate
after diagnosis when high-volume debug output is no longer wanted.

### 13.2 Current 2 Mbps diagnostic build used in the successful test

```powershell
Set-Location C:\DevTools\RENG\PROJ_3\makcu_stable

$env:PLATFORMIO_BUILD_FLAGS = '-DFIRMWARE_VERSION=\"V1_2_Pass_IDF\" -DCOM3_LOG=1 -DKM_DIAG=0 -DKM_RING=0 -DLAT_DIAG=0 -DCFG_TUSB_DEBUG_PRINTF=pass_tusb_debug_printf -Wl,--wrap=dcd_edpt_stall -Wl,--wrap=tud_descriptor_string_cb'
$env:PLATFORMIO_BUILD_UNFLAGS = '-DCOM3_LOG=0'

pio run -d firmware/MAKCM_ESP32s3_Pass_Left_IDF -e LEFT_IDF

Remove-Item Env:PLATFORMIO_BUILD_FLAGS -ErrorAction SilentlyContinue
Remove-Item Env:PLATFORMIO_BUILD_UNFLAGS -ErrorAction SilentlyContinue

pio run -d firmware/MAKCM_ESP32s3_Pass_Right -e RIGHT
```

### 13.3 Optional 5 Mbps comparison

This is a future A/B test, not the validated final configuration. Preserve the
working 2 Mbps images before doing it, and apply the override to both builds:

```powershell
Set-Location C:\DevTools\RENG\PROJ_3\makcu_stable

$env:PLATFORMIO_BUILD_FLAGS = '-DFIRMWARE_VERSION=\"V1_2_Pass_IDF\" -DCOM3_LOG=1 -DKM_DIAG=0 -DKM_RING=0 -DLAT_DIAG=0 -DCFG_TUSB_DEBUG_PRINTF=pass_tusb_debug_printf -Wl,--wrap=dcd_edpt_stall -Wl,--wrap=tud_descriptor_string_cb -DPASS_IPC_UART_BAUD=5000000UL'
$env:PLATFORMIO_BUILD_UNFLAGS = '-DCOM3_LOG=0'
pio run -d firmware/MAKCM_ESP32s3_Pass_Left_IDF -e LEFT_IDF -t clean
pio run -d firmware/MAKCM_ESP32s3_Pass_Left_IDF -e LEFT_IDF

Remove-Item Env:PLATFORMIO_BUILD_UNFLAGS -ErrorAction SilentlyContinue
$env:PLATFORMIO_BUILD_FLAGS = '-DPASS_IPC_UART_BAUD=5000000UL'
pio run -d firmware/MAKCM_ESP32s3_Pass_Right -e RIGHT -t clean
pio run -d firmware/MAKCM_ESP32s3_Pass_Right -e RIGHT

Remove-Item Env:PLATFORMIO_BUILD_FLAGS -ErrorAction SilentlyContinue
```

Flash both 5 Mbps images together. Never compare by changing only one MCU.

### 13.4 Successful build sizes

| Image | RAM | Flash program usage | `firmware.bin` size | SHA-256 |
|---|---:|---:|---:|---|
| Left diagnostic | 22,004 bytes (6.7%) | 317,989 bytes (10.1%) | 318,352 bytes | `A878A25FBE0C74CFDA9DD44D59234798C92432B0E09D1F7D74347A9C6D439BCF` |
| Right | 20,628 bytes (6.3%) | 327,901 bytes (10.4%) | 328,272 bytes | `BDA34F03A5DD883ED4466BDB45334518000A7A54A608D7421282E2B99436AA42` |

---

## 14. Flash procedure that succeeded

### 14.1 Port identities seen in this session

| Purpose | Session port | USB identity |
|---|---|---|
| Right ESP32-S3 bootloader | COM4 | `VID_303A&PID_1001` |
| Left ESP32-S3 bootloader | COM3 | `VID_303A&PID_0009` |
| CH343 UART0 diagnostics | COM5 | `VID_1A86&PID_55D3` |

COM numbers can change. Identify the device by VID/PID rather than assuming the
same COM number forever.

### 14.2 Right upload

Right was put in download mode and flashed first:

```powershell
Set-Location C:\DevTools\RENG\PROJ_3\makcu_stable\firmware\MAKCM_ESP32s3_Pass_Right
pio run -e RIGHT -t upload --upload-port COM4
```

The upload wrote and hash-verified:

- bootloader at `0x00000000`;
- partition table at `0x00008000`;
- boot application data at `0x0000E000`;
- application at `0x00010000`.

PlatformIO reported success and reset Right through RTS.

### 14.3 Left upload

Left was then put in download mode and flashed with the exact diagnostic build
flags:

```powershell
Set-Location C:\DevTools\RENG\PROJ_3\makcu_stable\firmware\MAKCM_ESP32s3_Pass_Left_IDF

$env:PLATFORMIO_BUILD_FLAGS = '-DFIRMWARE_VERSION=\"V1_2_Pass_IDF\" -DCOM3_LOG=1 -DKM_DIAG=0 -DKM_RING=0 -DLAT_DIAG=0 -DCFG_TUSB_DEBUG_PRINTF=pass_tusb_debug_printf -Wl,--wrap=dcd_edpt_stall -Wl,--wrap=tud_descriptor_string_cb'
$env:PLATFORMIO_BUILD_UNFLAGS = '-DCOM3_LOG=0'

pio run -e LEFT_IDF -t upload --upload-port COM3
```

All four regions were written and hash-verified. PlatformIO ended with
`Error 1` only because an ESP32-S3 entered through GPIO0 download mode cannot
exit that mode automatically over this USB path:

```text
chip was placed into download mode using GPIO0
esptool.py can not exit the download mode over USB
reset the chip manually
```

That final message was not a failed flash. The correct action was a manual
power cycle.

### 14.4 Cold power cycle and connection order

1. Disconnect all three USB connections for about ten seconds.
2. Connect USB3 to the XIM/controller.
3. Connect USB2/middle to the PC for COM5 diagnostics and board power.
4. Connect USB1/Left to Xbox last.
5. Open COM5 at 4,000,000 baud.
6. Confirm the target mount, announce replay, EP2 OUT, and `0x20` IN sequence.
7. Test physical controller movement and buttons on Xbox.

---

## 15. Diagnostic log reference

### Left IPC

```text
[L][IPC] UART1 up baud=2000000 ...
[L][IPC] CRC_FAIL count=... type=... len=... seq=...
```

### Left control state

```text
[L][CTL] PENDING_SET ...
[L][CTL] SETUP_FWD ...
[L][CTL] IN_DATA_RX ... match=1 ignored=0
[L][CTL] STATUS_RX ... match=1 ignored=0 pending=0
[L][CTL] BUSY ...
[L][CTL] STALE_TIMEOUT_CLEAR ...
```

### Left endpoint activity

```text
[L][EP] TARGET_RESET ...
[L][EP] TARGET_MOUNTED ...
[L][EP] IN ... ep=82 ...
[L][EP] OUT ... ep=02 ...
```

`[L][EP] IN` prints the first 20 real reports for an endpoint and then a
heartbeat every 1000 reports. `[L][EP] OUT` logs each observed target OUT
packet in the diagnostic build.

### Right tunneled activity

Right logs arrive on Left UART0 prefixed by `[R]`:

```text
[R] [IPC] Pass_Right boot baud=2000000
[R] [EP] GIP ANNOUNCE_CACHED ...
[R] [EP] TARGET_MOUNTED announce_cached=1
[R] [EP] GIP ANNOUNCE_REPLAY ... sent=1
[R] [CTL] SUBMIT ...
[R] [CTL] done ...
```

### TinyUSB enumeration

```text
[L][USB] USBD Bus Reset : Full Speed
[L][USB] USBD Setup Received ...
[L][USB] DESC_PROBE_SEND ...
[L][USB] DESC_PROBE_STATUS ...
[L][USB] MS_OS_STRING_MIRROR_REPLY index=ee ...
[L][USB] QUALIFIER_MIRROR_MISS -> STALL
[L][USB] QUALIFIER_STALL suppress_ep=00 allow_ep=80
```

---

## 16. How to diagnose a future regression

### Case A: no descriptors or `FRAME_DEVICE_READY`

Likely areas:

- Right did not enumerate the physical device;
- Left/Right baud mismatch;
- UART wiring or power issue;
- IPC CRC/frame loss before descriptor staging.

Check Right boot logs, descriptor-frame logs, and baud on both sides.

### Case B: Xbox stops before `SET_CONFIGURATION`

Check:

- `MS_OS_STRING_MIRROR_REPLY index=ee`;
- subsequent `0x90` vendor control requests;
- qualifier STALL direction;
- whether Left reached `tud_mount_cb`.

If `0xEE` is absent or incorrect, Xbox may never bind/configure the mirrored
controller.

### Case C: Xbox mounts and EP82 IN flows, but EP2 OUT remains zero

Check:

- `GIP ANNOUNCE_CACHED`;
- `TARGET_MOUNTED announce_cached=1`;
- `GIP ANNOUNCE_REPLAY ... sent=1`;
- a Left IN line whose payload begins with `02`;
- following `[L][EP] OUT` lines.

This is the signature of the final handshake-ordering issue.

### Case D: control requests become BUSY/STALL after an IPC error

Check for:

```text
[L][IPC] CRC_FAIL ... type=0x21
[L][CTL] STALE_TIMEOUT_CLEAR ...
```

The watchdog should clear the state after five seconds. A later matching-old
status should log `ignored=1` and must not disturb a new request.

### Case E: 5 Mbps fails but 2 Mbps works

Run identical cold-start tests with matched builds and compare:

- CRC failure count;
- time since last good frame;
- missed target-state frames;
- missed control status frames;
- EP2 OUT count;
- physical input behavior.

Do not draw a conclusion from builds where Left and Right rates differ.

---

## 17. Remaining risks and follow-up recommendations

### 17.1 Target mount/reset notification has no explicit acknowledgement

`FRAME_TARGET_MOUNTED` and `FRAME_TARGET_RESET` are CRC-protected but not
acknowledged or retried. At 2 Mbps no CRC failures occurred in the final run.
If future evidence shows a dropped mount frame, a small acknowledged state
message or periodic idempotent mount-state refresh would be safer than adding
general protocol complexity.

### 17.2 GIP announce cache is intentionally narrow

The replay logic keys on first byte `0x02` and stores up to 64 bytes, matching
the current full-speed endpoint path. This is correct for the tested
`045E:0B12` device and avoids the rejected global 65-byte rewrite. A different
protocol/device may need a different startup-event rule.

### 17.3 Descriptor workarounds are VID/PID-gated but version-sensitive

The qualifier and Microsoft string behavior is gated to the confirmed Xbox
mirror. The linker wrappers depend on the esp_tinyusb/TinyUSB symbols and
behavior in the current framework versions. Revalidate them after upgrading
ESP-IDF, esp_tinyusb, TinyUSB, or PlatformIO packages.

### 17.4 Diagnostic logging should be reduced after soak testing

The successful Left image uses `COM3_LOG=1` and TinyUSB level 2. This is useful
for proof but creates extra UART traffic and CPU work. After a longer gameplay
soak test, rebuild Left with the checked-in quiet default (`COM3_LOG=0`) and
optionally return TinyUSB debug level to 1. Keep the functional string/stall
wrappers and GIP sequencing changes.

### 17.5 Five Mbps remains an experiment

Do not replace the validated 2 Mbps pair until a matched 5 Mbps pair completes
the same cold-start and input test with no rising CRC count.

### 17.6 Watchdog recovery does not cancel a physical control URB

The Left watchdog clears its local state. A very late physical completion on
Right can still arrive, but its sequence is rejected safely on Left. If a
future device holds control URBs for unusually long periods, Right-side URB
cancellation/timeout behavior may deserve separate analysis.

### 17.7 Soak-test recommendations

Before declaring the build production-final, test:

- multiple full Xbox cold boots;
- Xbox sleep/wake and controller reconnect;
- XIM/controller unplug/replug while both MCUs remain powered;
- at least one long gameplay session;
- repeated button/stick activity plus any KM injection path in use;
- COM5 monitoring for CRC growth, stale timeouts, resets, or endpoint stalls.

---

## 18. Rollback and recovery

### 18.1 Verified pre-flash source backup

The patched source was archived before flashing:

```text
C:\Users\mrqui\Documents\Projects\shared\makcu_stable-gip-sequencing-preflash-20260712.zip
```

Archive properties:

```text
SHA-256: FD5741639CB12AD2A7648BDB38C1F280C28F75CAF6872DA0D4C05A11B2677FC9
Files:   1,835
Size:    approximately 3.54 MB
```

The archive was verified to contain the required Left/Right C/C++ sources,
both IPC headers, PlatformIO configurations, both Left sdkconfig files, and
`PATCH_SUMMARY.md`. Required-file hashes matched the working source. `.pio`
build-output directories were intentionally excluded.

### 18.2 Baud-only rollback

No source edit is required. Rebuild and flash both projects with:

```text
-DPASS_IPC_UART_BAUD=5000000UL
```

This restores the original link rate but retains all control, descriptor, and
GIP fixes.

### 18.3 Remove only the final GIP sequencing fix

To return to the pre-final behavior:

1. Remove `FRAME_TARGET_MOUNTED` and `FRAME_TARGET_RESET` from both
   `pass_ipc.h` copies.
2. Remove target-state sends from Left `pass_usb_device.c`.
3. Remove the two cases from Right `main.cpp`.
4. Restore the old `gip_send()` / `gip_kickstart()` declarations and
   implementation in Right `PassUsbHost.h/.cpp`.
5. Remove announce-cache/target-gating state.
6. Clean, rebuild, and flash both MCUs together.

This rollback is not recommended for Xbox use because it recreates the proven
zero-EP2-OUT failure.

### 18.4 Remove only descriptor diagnostics/workarounds

- Remove `--wrap=tud_descriptor_string_cb` and the `0xEE` probe/cache to undo
  the Microsoft OS string mirror. This is expected to break Xbox enumeration
  for the tested device.
- Remove `--wrap=dcd_edpt_stall` and the qualifier-specific wrapper to undo the
  EP0 direction guard.
- Set TinyUSB debug level to 1 to remove the high-volume trace without
  changing functional descriptor behavior.

### 18.5 Full source rollback

Restore the original clean source archive referenced by the project's
`PATCH_SUMMARY.md`, then clean and rebuild both projects. Always flash both
sides when reverting shared IPC definitions.

---

## 19. Final conclusions

The successful result came from treating the passthrough as a sequence of
separate protocols rather than one generic “USB is broken” problem:

1. The HID API's 65-byte buffer did not justify changing the 64-byte USB
   transaction path.
2. The stale control flag was a real robustness defect and received a bounded,
   independent recovery path.
3. Windows enumeration proved the core device/IPC/IN path.
4. Xbox's `0xEE` request revealed the missing enumeration requirement.
5. Reaching mount but seeing zero EP2 OUT isolated a post-enumeration protocol
   problem.
6. The initial GIP announce was being consumed on the wrong side of the
   bridge.
7. Replaying the physical announce after Left mount restored ownership of the
   handshake to Xbox.
8. Xbox then emitted real EP2 OUT traffic, and the user confirmed movement.

The current validated configuration is therefore:

```text
Matched Left/Right IPC: 2,000,000 baud
Left diagnostic UART0: 4,000,000 baud on CH343 COM5
Control timeout:        5 seconds, independent watchdog
Microsoft OS 0xEE:      exact physical descriptor mirror
Qualifier:              exact physical STALL behavior, correct EP0 direction
GIP initialization:     Xbox-owned, real announce cached/replayed after mount
USB packet buffers:     unchanged 64-byte endpoint payload path
Functional result:      Xbox passthrough movement confirmed
```

This is the baseline that should be preserved before any performance tuning,
logging reduction, framework upgrade, or 5 Mbps comparison.
