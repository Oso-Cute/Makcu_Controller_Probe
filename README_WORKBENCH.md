# Makcu Development Workbench

This is the clean working copy. Make new firmware changes here.

## Baseline

- Source copied from `..\macku_controller` on July 12, 2026.
- The copied working tree contains the verified Xbox passthrough recovery changes.
- Xbox Series X controller movement was confirmed through the complete passthrough chain.
- Validated inter-MCU IPC rate: 2,000,000 baud on both sides.
- No `.pio` build output, downloaded `managed_components`, old Git history, or generated merged images were copied into this workbench.
- The complete recovery report is in `workspace_notes\MAKCU_XBOX_PASSTHROUGH_RECOVERY_2026-07-12.md`.

## Git safety

The immutable starting point is tagged:

```text
xbox-working-2026-07-12
```

Development takes place on:

```text
g7-pro-support
```

Useful checks:

```powershell
git status
git diff
git log --oneline --decorate -5
```

To compare current work with the known-good baseline:

```powershell
git diff xbox-working-2026-07-12
```

Do not use destructive Git commands against the reference release folders. If an experiment fails, keep the evidence and return this workbench to the baseline through a new branch or a deliberate revert.

## Build locations

Left firmware:

```text
firmware\MAKCM_ESP32s3_Pass_Left_IDF
```

Right firmware:

```text
firmware\MAKCM_ESP32s3_Pass_Right
```

PlatformIO will regenerate excluded dependencies and build output on the first build.

## Protected material

Release archives and known-good binaries are stored outside this Git repository in:

```text
..\_protected_releases
```

Treat that folder and all other sibling source trees as read-only references.

