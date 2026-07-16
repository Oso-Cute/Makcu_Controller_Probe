# Probe Firmware Design

The normal `LEFT_IDF` and `RIGHT` build environments remain unchanged.
Controller tracing is compiled only by `LEFT_PROBE` and `RIGHT_PROBE`.

## Structured record format

Records use an ASCII, space-separated `key=value` grammar and contain the
marker `[PRB]`. Values never contain spaces.

Important records:

```text
[PRB] HELLO ...
[PRB] DEVICE ...
[PRB] CONFIG ...
[PRB] IF ...
[PRB] EP ...
[PRB] BLOB record=... kind=... id=... off=... total=... hex=...
[PRB] PKT id=... side=R dir=... phase=... ep=... off=... total=... hex=...
[PRB] CTRL ...
[PRB] STATE ...
[PRB] PROBE_STATUS ...
[PRB] ERROR ...
```

Binary values are split into independently parseable 32-byte chunks. The PC
collector reassembles them by device generation, record, kind, ID, offset, and
total length.

## Packet sampling bounds

- Up to 16 physical IN packets are recorded before target mount.
- After mount, only payload changes are recorded.
- At most 128 physical IN samples are recorded per controller enumeration.
- At most 64 bytes are retained from an endpoint packet.

These bounds prevent constant-rate 1 kHz controllers from saturating the
2 Mbps IPC link and changing the handshake being measured.

## Diagnostic descriptor probes

`LEFT_PROBE` requests Microsoft OS string `0xEE` and the device qualifier from
every attached controller. This is evidence collection only: production
descriptor-mirroring callbacks remain gated to their previously validated
VID/PID, so probe mode does not silently broaden production behavior.

## Probe handshake

The PC sends `km.probe()` over the CH343 command channel. Left replies through
the always-on UART writer, then sends `FRAME_PROBE_COMMAND/PROBE_CMD_HELLO` to
Right. A valid capture therefore contains matching `probe=1` banners from both
MCUs and the same IPC baud.

## Optional G7 startup experiment

`km.g7kick()` is compiled only in `LEFT_PROBE`/`RIGHT_PROBE`. It is an explicit
evidence-gathering action, never an automatic compatibility behavior. Right
accepts the request once per enumeration only when the physical controller is
the GameSir G7 Pro (`VID:PID 3537:1003`) and its interrupt-OUT endpoint was
found in the active configuration. It submits the legacy v0.1 identify,
power-on, and LED-on GIP packets, then records their submit/completion results.
After the G7 light appears, a **second** `km.g7kick()` sends no controller
packet; it arms a 128-record, 10 Hz exact-raw capture (about 12.8 seconds) for
the operator's stick/button actions. Pacing is required because this G7 changes
a report-counter byte in every `0x20` input and would otherwise exhaust a
change-only sampler in under a second. Normal firmware cannot send either
diagnostic action.

## G7 Xbox descriptor-mirror experiment

`LEFT_PROBE` has one deliberately narrow descriptor experiment. While the
physical device is final GameSir G7 Pro `3537:1003`, it mirrors that device's
captured MS OS `0xEE` string and preserves its physical device-qualifier STALL
direction for Xbox. The hot-plug trace measured both behaviors. The normal
`LEFT_IDF` build remains limited to the validated `045E:0B12` identity.

The experiment does not synthesize GIP traffic, alter G7 endpoint packets, or
broaden behavior for another controller.

## Merged-image layout

The packaging script merges:

```text
0x00000000  bootloader.bin
0x00008000  partitions.bin
0x0000E000  boot_app0.bin
0x00010000  firmware.bin
```
