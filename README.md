# MAKCU Controller Probe

This was 100% done with the help of Claude.

💬 **Community:** [Join The S&Box Discord](https://discord.gg/hPFZbJwY2Z) — come share what you're building with Titan2 and makcu/controller.

☕ **Support:** [Buy me a coffee](https://buymeacoffee.com/OsoCute) — if this project helped you out.

## Demo video

*Coming soon.*

<!--
[![MAKCU Controller Probe — demo](https://img.youtube.com/vi/VIDEO_ID/maxresdefault.jpg)](https://youtu.be/VIDEO_ID)
-->

## What it does

A guided capture tool for the dual-ESP32-S3 MAKCU/MAKCM passthrough board. It
records a controller's USB descriptors, host enumeration, handshake packets,
and input reports into a single ZIP — the evidence a developer needs to add
support for a controller they don't physically own.

It does **not** make an unsupported controller work by itself. It removes the
guesswork from the firmware change that will.

## Quick start

1. Back up your current Left/Right firmware.
2. Flash both probe images: `tools\controller_probe\Flash_Probe_Firmware.bat`
   (manual esptool steps in `tools/controller_probe/MANUAL_FLASH.md`).
3. Disconnect all cables for ten seconds.
4. Run `tools\controller_probe\Run_Controller_Probe.bat` and follow the
   prompts.
5. Send the `SEND_TO_OSO_CUTE_...zip` from `Controller_Probe_Reports`
   privately to oso_cute — it can contain controller serial/auth traffic, so
   don't post it publicly.

Full walkthrough: [`tools/controller_probe/README.md`](tools/controller_probe/README.md)

## Layout

```text
tools/controller_probe/   Guided probe tool, flasher, and docs
firmware/                 Left/Right passthrough firmware sources + flash tool
firmware/rawbins/         Prebuilt merged probe images
accessibility/            Accessibility command GUI for the CH343 port
FLASHING.md               Detailed flashing instructions
```

## Related

- [makcu-controller-fw](https://github.com/Oso-Cute/makcu-controller-fw) —
  the community-fixed MAKCM passthrough firmware this probe supports.

## License

See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md).
