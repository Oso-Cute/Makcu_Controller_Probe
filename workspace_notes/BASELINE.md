# Workbench Baseline

## Source identity

- Reference tree: local `macku_controller` working copy
- Reference Git branch at copy time: `test-gui`
- Reference `HEAD` at copy time: `ef53244`
- Important: the verified Xbox recovery changes were uncommitted modifications on top of that commit. The workbench copied the actual working files, not merely `HEAD`.

## Known-good behavior

- Xbox reaches `SET_CONFIGURATION`.
- The physical GIP announce is cached on Right and replayed after Left mounts.
- Xbox sends real EP2 OUT traffic through the bridge.
- Controller movement reaches Xbox.
- The validated 25-second diagnostic capture recorded 54 EP2 OUT packets, zero IPC CRC failures, and zero stale-control timeout events.

## First work item

Generalize controller discovery and handshake handling for the GameSir G7 Pro without regressing the tagged Xbox-working baseline.

