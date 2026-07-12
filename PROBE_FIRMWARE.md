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

## Merged-image layout

The packaging script merges:

```text
0x00000000  bootloader.bin
0x00008000  partitions.bin
0x0000E000  boot_app0.bin
0x00010000  firmware.bin
```

