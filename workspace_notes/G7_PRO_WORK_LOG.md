# GameSir G7 Pro Work Log

Keep each test small and record the result before changing anything else.

## Controller identity

- Exact product/variant:
- Rear-label model number:
- Controller firmware version:
- Connection used: direct USB cable / receiver
- Selected controller mode:
- USB VID:PID:
- `bcdDevice`:

## Descriptor and endpoint capture

- Configuration descriptor length:
- Interface count:
- IN endpoints and maximum packet sizes:
- OUT endpoints and maximum packet sizes:
- Microsoft OS string `0xEE` result:
- Device qualifier result:
- First pre-mount IN packets:

## Failure stage before changes

- [ ] Right detects the controller (`NEW_DEV`)
- [ ] Right relays device/configuration descriptors
- [ ] Left exposes D+ to Xbox
- [ ] Xbox reaches `SET_CONFIGURATION`
- [ ] Left reports `TARGET_MOUNTED`
- [ ] Right caches and replays the startup announce
- [ ] Xbox sends endpoint OUT traffic
- [ ] GIP input reports beginning with `0x20` reach Left
- [ ] Physical movement reaches Xbox
- [ ] KM injection modifies the correct input report

## Experiments

| Date/time | Git commit | Change | Left image SHA-256 | Right image SHA-256 | Result | Keep/revert |
|---|---|---|---|---|---|---|
| | `xbox-working-2026-07-12` | Known-good Xbox baseline | `A30F918E...` merged image | `3B958240...` merged image | Xbox passthrough verified | Keep as rollback |

## Notes

- Flash both sides together whenever shared IPC definitions change.
- Do not overwrite the files in `..\..\_protected_releases`.
- Preserve the first failing COM capture; it is usually more informative than later traces after several changes.

