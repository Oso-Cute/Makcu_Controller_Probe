# PR: Add Shareable Controller Probe Firmware and Report Collector

## Summary

Adds dedicated `LEFT_PROBE` and `RIGHT_PROBE` build environments plus a guided
Windows/Python collector that creates an email-ready compatibility bundle for
new controllers such as the GameSir G7 Pro.

The normal `LEFT_IDF` and `RIGHT` environments retain their existing build
flags. The known-good rollback tag remains `xbox-working-2026-07-12`.

## Firmware changes

- Adds a matched two-MCU `km.probe()`/IPC handshake.
- Emits chunked exact device/configuration/string descriptor bytes.
- Emits structured interface, alternate-setting, and endpoint metadata.
- Diagnostically probes Microsoft OS `0xEE`, qualifier, and other-speed
  behavior for every controller in probe builds.
- Captures up to 16 pre-mount IN packets.
- Captures change-only post-mount IN packets, bounded to 128 samples.
- Bounds endpoint OUT packet/result samples to 128.
- Records control requests/responses, mount/reset, announce cache/replay,
  CRC errors, and stale-control recovery.
- Submits/forwards real USB traffic before formatting diagnostic records on
  timing-sensitive paths.

## Collector changes

- Auto-detects the CH343 command port at 4,000,000 baud.
- Verifies matching Left/Right probe banners and IPC baud.
- Guides physical enumeration, Xbox enumeration, handshake, and input mapping.
- Reassembles chunked binary records and analyzes the failure stage.
- Produces a full private developer ZIP and a public-redacted ZIP.
- Supports offline re-analysis of an emailed `raw_serial.log`.
- Includes a guided probe flasher and reproducible package builder.

## Automated verification

- `LEFT_PROBE`: build successful; 22,004 bytes RAM; 319,453 bytes program
  usage; application binary 319,824 bytes.
- `RIGHT_PROBE`: build successful; 21,684 bytes RAM; 332,557 bytes program
  usage; application binary 332,928 bytes.
- Normal `LEFT_IDF`: build successful.
- Normal `RIGHT`: build successful.
- Python parser/report tests: 4 passed.
- Python compile check: passed.
- Git whitespace check: passed.

## Hardware test still required

The first hardware validation target is the GameSir G7 Pro in direct
USB/Xbox-wired mode. Confirm:

1. both probe banners appear;
2. complete descriptors are reconstructed;
3. `0xEE`/qualifier results are recorded;
4. Xbox mount and OUT handshake state is correctly classified;
5. guided control actions produce bounded change-only samples;
6. both ZIPs are generated and the public ZIP contains no raw packet/control
   bytes or serial string.

## Known scope

- The ESP32-S3 path is full-speed and captures at most 64 bytes per endpoint
  packet.
- The active configuration descriptor is captured exactly; HID report
  descriptors are not yet fetched through a dedicated probe request.
- Probe builds collect behavior but deliberately do not generalize production
  descriptor-mirroring rules.
- Full developer bundles may contain authentication traffic and must be shared
  privately.

