#!/usr/bin/env python3
"""MAKCU Controller Probe collector and report generator.

The live workflow talks to the CH343 middle port at 4 Mbaud, verifies that
LEFT_PROBE and RIGHT_PROBE are installed, guides a controller/host capture,
then creates a full developer bundle containing the raw trace and exact USB
bytes. The bundle may contain a controller serial number or authentication
traffic, so it is meant to be sent privately, not posted publicly.

An existing raw log can be re-analyzed with ``--analyze path.log`` without
hardware or pyserial.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


TOOL_VERSION = "1.2.0"
PROBE_SCHEMA = 1
KM_BAUD = 4_000_000
CH343_VID = 0x1A86
HEX_FIELDS = {
    "vid", "pid", "bcd", "usb", "class", "subclass", "protocol",
    "address", "attributes", "bm", "request", "value", "index",
    "expected", "received", "frame_type", "ep",
}

try:  # Parser/tests/offline analysis do not require pyserial.
    import serial  # type: ignore
    import serial.tools.list_ports  # type: ignore
except ImportError:  # pragma: no cover - exercised only on machines without it
    serial = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._-") or "unknown_controller"


def parse_kv(text: str) -> dict[str, str]:
    """Parse the probe's deliberately space-free key=value grammar."""
    result: dict[str, str] = {}
    for token in text.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        result[key] = value
    return result


def maybe_int(value: Any, *, hex_value: bool = False) -> Any:
    if not isinstance(value, str):
        return value
    try:
        if hex_value:
            return int(value, 16)
        return int(value, 10)
    except (TypeError, ValueError):
        return value


def decode_hex(value: str) -> bytes:
    if not value or value == "-":
        return b""
    if len(value) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", value):
        return b""
    return bytes.fromhex(value)


def decode_usb_string(data: bytes) -> str:
    if len(data) >= 2 and data[1] == 0x03 and data[0] <= len(data):
        data = data[2:data[0]]
    try:
        return data.decode("utf-16-le", errors="replace").rstrip("\x00")
    except Exception:
        return ""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


class ProbeParser:
    """Incrementally parse structured probe records plus legacy diagnostics."""

    def __init__(self) -> None:
        self.generation = 0
        self.hellos: dict[str, dict[str, str]] = {}
        self.devices: dict[int, dict[str, str]] = {}
        self.configs: dict[int, dict[str, str]] = {}
        self.interfaces: list[dict[str, Any]] = []
        self.endpoints: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.controls: list[dict[str, Any]] = []
        self.out_results: list[dict[str, Any]] = []
        self.probe_status: list[dict[str, Any]] = []
        self.blobs: dict[tuple[int, str, str, str], dict[str, Any]] = {}
        self.packets: dict[tuple[int, str], dict[str, Any]] = {}
        self.legacy_packets: list[dict[str, Any]] = []
        self.legacy: dict[str, Any] = {
            "crc_fail": 0,
            "stale_timeout": 0,
            "target_mounted": 0,
            "announce_cached": 0,
            "announce_replay": 0,
            "device_seen": False,
            "device": {},
            "config_total": None,
            "telemetry_samples": 0,
        }
        self.unparsed_probe_lines: list[str] = []

    @staticmethod
    def _typed(kv: dict[str, str]) -> dict[str, Any]:
        typed: dict[str, Any] = {}
        decimal_fields = {
            "schema", "probe", "ipc_baud", "address", "ep0",
            "config_value", "configs", "mfg_index", "product_index",
            "serial_index", "total", "interfaces", "max_power",
            "string_index", "number", "alt", "endpoints", "mps",
            "interval", "off", "id", "status", "t_us", "len",
            "captured", "requested", "actual", "seq", "data_len",
            "out_len", "sent", "announce_cached", "announce_len",
            "descriptor_probe", "ms_os_valid", "qualifier_valid",
            "other_speed_valid", "count", "last_good_age_ms",
        }
        for key, value in kv.items():
            if key in decimal_fields:
                typed[key] = maybe_int(value)
            elif key in HEX_FIELDS:
                typed[key] = maybe_int(value, hex_value=True)
            else:
                typed[key] = value
        return typed

    def process_line(self, line: str) -> None:
        marker = line.find("[PRB]")
        if marker >= 0:
            payload = line[marker + len("[PRB]"):].strip()
            event, _, rest = payload.partition(" ")
            kv_raw = parse_kv(rest)
            kv = self._typed(kv_raw)
            if event == "EP" and "address" in kv_raw:
                # Endpoint addresses are emitted as two-digit hexadecimal;
                # DEVICE.address is the decimal USB bus address.
                kv["address"] = maybe_int(kv_raw["address"], hex_value=True)

            if event == "STATE" and kv.get("event") == "new_device":
                self.generation += 1
            gen = self.generation
            kv["generation"] = gen

            if event == "HELLO":
                self.hellos[str(kv.get("side", "?"))] = kv_raw
            elif event == "DEVICE":
                self.devices[gen] = kv_raw
            elif event == "CONFIG":
                self.configs[gen] = kv_raw
            elif event == "IF":
                self.interfaces.append(kv)
            elif event == "EP":
                self.endpoints.append(kv)
            elif event == "STATE":
                self.states.append(kv)
            elif event == "ERROR":
                self.errors.append(kv)
            elif event == "CTRL":
                self.controls.append(kv)
            elif event == "OUT_RESULT":
                self.out_results.append(kv)
            elif event == "PROBE_STATUS":
                self.probe_status.append(kv)
            elif event == "BLOB":
                key = (gen, str(kv.get("record", "unknown")),
                       str(kv.get("kind", "unknown")), str(kv.get("id", "0")))
                entry = self.blobs.setdefault(key, {
                    "generation": gen,
                    "record": str(kv.get("record", "unknown")),
                    "kind": str(kv.get("kind", "unknown")),
                    "id": str(kv.get("id", "0")),
                    "total": int(kv.get("total", 0) or 0),
                    "chunks": {},
                })
                entry["total"] = max(entry["total"], int(kv.get("total", 0) or 0))
                entry["chunks"][int(kv.get("off", 0) or 0)] = decode_hex(
                    str(kv_raw.get("hex", "")))
            elif event == "PKT":
                key = (gen, str(kv.get("id", "0")))
                entry = self.packets.setdefault(key, {
                    **{k: v for k, v in kv.items() if k not in {"off", "hex"}},
                    "chunks": {},
                })
                entry.update({k: v for k, v in kv.items()
                              if k not in {"off", "hex"}})
                entry["chunks"][int(kv.get("off", 0) or 0)] = decode_hex(
                    str(kv_raw.get("hex", "")))
            else:
                self.unparsed_probe_lines.append(payload)

        self._process_legacy(line)

    def _process_legacy(self, line: str) -> None:
        m = re.search(
            r"dev VID=([0-9a-fA-F]{4}) PID=([0-9a-fA-F]{4}).*bcdDev=([0-9a-fA-F]{4})",
            line,
        )
        if m:
            self.legacy["device_seen"] = True
            self.legacy["device"] = {"vid": m.group(1).lower(),
                                     "pid": m.group(2).lower(),
                                     "bcd": m.group(3).lower()}
        m = re.search(r"cfg wTotal=(\d+) nIf=(\d+)", line)
        if m:
            self.legacy["config_total"] = int(m.group(1))
        if "CRC_FAIL" in line:
            self.legacy["crc_fail"] += 1
        if "STALE_TIMEOUT_CLEAR" in line:
            self.legacy["stale_timeout"] += 1
        if "TARGET_MOUNTED" in line or "tud_mount_cb" in line:
            self.legacy["target_mounted"] += 1
        if "ANNOUNCE_CACHED" in line:
            self.legacy["announce_cached"] += 1
        if "ANNOUNCE_REPLAY" in line:
            self.legacy["announce_replay"] += 1
        if "KMS " in line:
            self.legacy["telemetry_samples"] += 1

        m = re.search(
            r"\[L\]\[EP\]\s+(IN|OUT).*?ep=([0-9a-fA-F]{2})\s+len=(\d+)\s+hex=([0-9a-fA-F]+)",
            line,
        )
        if m:
            self.legacy_packets.append({
                "generation": self.generation,
                "side": "L",
                "dir": m.group(1).lower(),
                "phase": "legacy",
                "ep": int(m.group(2), 16),
                "len": int(m.group(3)),
                "data": decode_hex(m.group(4)),
            })

    @staticmethod
    def _assemble(total: int, chunks: dict[int, bytes]) -> tuple[bytes, bool]:
        if total <= 0:
            return b"", True
        output = bytearray(total)
        covered = bytearray(total)
        for off, chunk in chunks.items():
            if off < 0 or off >= total:
                continue
            take = min(len(chunk), total - off)
            output[off:off + take] = chunk[:take]
            covered[off:off + take] = b"\x01" * take
        return bytes(output), all(covered)

    def build_profile(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        metadata = metadata or {}
        generation = max([0, *self.devices.keys(), *self.configs.keys()])
        device = dict(self.devices.get(generation, self.legacy.get("device", {})))
        config = dict(self.configs.get(generation, {}))

        blobs: list[dict[str, Any]] = []
        strings: list[dict[str, Any]] = []
        for key in sorted(self.blobs):
            entry = self.blobs[key]
            data, complete = self._assemble(entry["total"], entry["chunks"])
            item = {
                "generation": entry["generation"],
                "record": entry["record"],
                "kind": entry["kind"],
                "id": entry["id"],
                "length": len(data),
                "complete": complete,
                "sha256": sha256_hex(data),
                "hex": data.hex(),
            }
            blobs.append(item)
            if (entry["record"] == "desc" and entry["kind"] == "string" and
                    entry["generation"] == generation):
                strings.append({
                    "index": maybe_int(entry["id"]),
                    "value": decode_usb_string(data),
                    "sha256": sha256_hex(data),
                    "hex": data.hex(),
                })

        packets: list[dict[str, Any]] = []
        for key in sorted(self.packets):
            entry = self.packets[key]
            total = int(entry.get("total", entry.get("captured", 0)) or 0)
            data, complete = self._assemble(total, entry["chunks"])
            packets.append({
                **{k: v for k, v in entry.items() if k != "chunks"},
                "complete": complete,
                "sha256": sha256_hex(data),
                "hex": data.hex(),
                "first_byte": data[0] if data else None,
            })
        for index, entry in enumerate(self.legacy_packets, start=1):
            data = entry["data"]
            packets.append({
                **{k: v for k, v in entry.items() if k != "data"},
                "id": f"legacy-{index}",
                "captured": len(data),
                "total": len(data),
                "complete": True,
                "sha256": sha256_hex(data),
                "hex": data.hex(),
                "first_byte": data[0] if data else None,
            })

        current_interfaces = [x for x in self.interfaces
                              if x.get("generation") == generation]
        current_endpoints = [x for x in self.endpoints
                             if x.get("generation") == generation]
        state_names = [str(x.get("event", "")) for x in self.states]
        mounted = ("target_mounted" in state_names or
                   self.legacy["target_mounted"] > 0)
        announce = ("announce_cached" in state_names or
                    self.legacy["announce_cached"] > 0 or
                    any(p.get("dir") == "in" and p.get("first_byte") == 0x02
                        for p in packets))
        replay = ("announce_replay" in state_names or
                  self.legacy["announce_replay"] > 0)
        out_packets = [p for p in packets if p.get("dir") == "out"]
        in_packets = [p for p in packets if p.get("dir") == "in"]
        gip_inputs = [p for p in in_packets if p.get("first_byte") == 0x20]
        crc_failures = self.legacy["crc_fail"] + sum(
            1 for e in self.errors if e.get("type") == "ipc_crc")
        stale_timeouts = self.legacy["stale_timeout"]

        left_hello = self.hellos.get("L", {})
        right_hello = self.hellos.get("R", {})
        probe_pair_valid = (
            left_hello.get("probe") == "1" and
            right_hello.get("probe") == "1" and
            left_hello.get("schema") == str(PROBE_SCHEMA) and
            right_hello.get("schema") == str(PROBE_SCHEMA) and
            left_hello.get("ipc_baud") == right_hello.get("ipc_baud")
        )
        if not probe_pair_valid:
            verdict = "The matched probe firmware pair was not confirmed."
            failure_stage = "probe_firmware_or_ipc"
        elif not device:
            verdict = "No downstream controller descriptor was captured."
            failure_stage = "controller_to_right"
        elif not config and not any(b["kind"] == "config" for b in blobs):
            verdict = "Controller was detected, but its configuration descriptor was not captured."
            failure_stage = "descriptor_relay"
        elif not mounted:
            verdict = "Descriptors were captured, but the host device never mounted the mirrored controller."
            failure_stage = "host_enumeration"
        elif not out_packets:
            verdict = "The host device mounted the controller but no endpoint-OUT handshake traffic was captured."
            failure_stage = "startup_handshake"
        elif not gip_inputs:
            verdict = "The host device sent OUT traffic, but no standard GIP 0x20 input report was captured."
            failure_stage = "authentication_or_report_format"
        else:
            verdict = "GIP announce, host OUT traffic, and 0x20 input reports were captured."
            failure_stage = "none_observed"

        report = {
            "schema": PROBE_SCHEMA,
            "tool": {"name": "MAKCU Controller Probe", "version": TOOL_VERSION},
            "created_utc": utc_now(),
            "metadata": metadata,
            "firmware": {"left": left_hello, "right": right_hello},
            "generation_analyzed": generation,
            "device": device,
            "config": config,
            "strings": strings,
            "interfaces": current_interfaces,
            "endpoints": current_endpoints,
            "descriptor_blobs": blobs,
            "packets": packets,
            "controls": self.controls,
            "out_results": self.out_results,
            "states": self.states,
            "probe_status": self.probe_status,
            "errors": self.errors,
            "legacy_counters": self.legacy,
            "analysis": {
                "verdict": verdict,
                "failure_stage": failure_stage,
                "probe_pair_valid": probe_pair_valid,
                "controller_detected": bool(device),
                "target_mounted": mounted,
                "announce_seen": announce,
                "announce_replayed": replay,
                "out_packet_count": len(out_packets),
                "in_packet_count": len(in_packets),
                "gip_input_count": len(gip_inputs),
                "crc_failure_count": crc_failures,
                "stale_timeout_count": stale_timeouts,
                "packet_first_bytes": {
                    f"0x{k:02x}": v for k, v in sorted(Counter(
                        p["first_byte"] for p in packets
                        if p.get("first_byte") is not None).items())
                },
                "packet_lengths": dict(sorted(Counter(
                    str(p.get("len", p.get("captured", 0)))
                    for p in packets).items())),
            },
        }
        return report


def h(value: Any, width: int = 4) -> str:
    if isinstance(value, int):
        return f"{value:0{width}X}"
    if isinstance(value, str):
        return value.upper()
    return "unknown"


def make_markdown_report(profile: dict[str, Any]) -> str:
    analysis = profile["analysis"]
    device = profile.get("device", {})
    metadata = profile.get("metadata", {})
    strings = {x.get("index"): x.get("value", "")
               for x in profile.get("strings", [])}
    mfg_idx = maybe_int(device.get("mfg_index", -1))
    product_idx = maybe_int(device.get("product_index", -1))
    serial_idx = maybe_int(device.get("serial_index", -1))
    left = profile.get("firmware", {}).get("left", {})
    right = profile.get("firmware", {}).get("right", {})

    lines = [
        "# MAKCU Controller Probe Report",
        "",
        f"**Created (UTC):** {profile.get('created_utc', 'unknown')}  ",
        f"**Controller label:** {metadata.get('controller_name', 'unknown')}  ",
        f"**Variant/model:** {metadata.get('model', 'unknown')}  ",
        f"**USB1 host:** {metadata.get('host_device', 'unknown')}  ",
        f"**Capture sequence:** {metadata.get('capture_sequence', 'unknown')}  ",
        f"**Connection/mode:** {metadata.get('connection_mode', 'unknown')}  ",
        "",
        "## Automated result",
        "",
        f"**{analysis['verdict']}**",
        "",
        f"Failure stage: `{analysis['failure_stage']}`",
        "",
        "| Check | Result |",
        "|---|---:|",
        f"| Matched probe firmware pair | {'Yes' if analysis['probe_pair_valid'] else 'No'} |",
        f"| Controller descriptor captured | {'Yes' if analysis['controller_detected'] else 'No'} |",
        f"| Host device mounted controller | {'Yes' if analysis['target_mounted'] else 'No'} |",
        f"| Startup announce seen | {'Yes' if analysis['announce_seen'] else 'No'} |",
        f"| Announce replayed | {'Yes' if analysis['announce_replayed'] else 'No'} |",
        f"| Endpoint OUT packets | {analysis['out_packet_count']} |",
        f"| Standard GIP `0x20` inputs | {analysis['gip_input_count']} |",
        f"| IPC CRC failures | {analysis['crc_failure_count']} |",
        f"| Stale-control recoveries | {analysis['stale_timeout_count']} |",
        "",
    ]

    # Per-phase checkpoint timing (present in live captures from tool 1.1+).
    # announce_to_mount_ms < 0 with no replay is the replay-race signature:
    # the host mounted before the controller's announce was cached.
    checkpoints = metadata.get("phase_checkpoints") or []
    if checkpoints:
        lines += [
            "## Handshake timing per phase",
            "",
            "| Phase | Stage reached | Announce→mount (ms) | Replay | Host OUT |",
            "|---|---|---:|---|---:|",
        ]
        for cp in checkpoints:
            ms = cp.get("milestones", {})
            delta = cp.get("announce_to_mount_ms")
            lines.append(
                f"| {cp.get('phase', '?')} | `{cp.get('stage', '?')}` | "
                f"{delta if delta is not None else '—'} | "
                f"{'Yes' if ms.get('announce_replay') else 'No'} | "
                f"{cp.get('counts', {}).get('host_out', 0)} |"
            )
        lines.append("")

    lines += [
        "## Device identity",
        "",
        f"- VID:PID: `{h(maybe_int(device.get('vid'), hex_value=True))}:{h(maybe_int(device.get('pid'), hex_value=True))}`",
        f"- bcdDevice: `{h(maybe_int(device.get('bcd'), hex_value=True))}`",
        f"- USB version: `{h(maybe_int(device.get('usb'), hex_value=True))}`",
        f"- Speed: `{device.get('speed', 'unknown')}`",
        f"- Manufacturer: `{strings.get(mfg_idx, 'not captured')}`",
        f"- Product: `{strings.get(product_idx, 'not captured')}`",
        f"- Serial: `{'[REDACTED IN REPORT]' if isinstance(serial_idx, int) and serial_idx > 0 else 'not present'}`",
        "",
        "## Interfaces and endpoints",
        "",
        "| Interface | Alt | Class/Subclass/Protocol | Endpoints |",
        "|---:|---:|---|---:|",
    ]
    for interface in profile.get("interfaces", []):
        lines.append(
            f"| {interface.get('number', '?')} | {interface.get('alt', '?')} | "
            f"`{h(interface.get('class'), 2)}/{h(interface.get('subclass'), 2)}/{h(interface.get('protocol'), 2)}` | "
            f"{interface.get('endpoints', '?')} |"
        )
    if not profile.get("interfaces"):
        lines.append("| — | — | No structured interface records | — |")

    lines += [
        "",
        "| Endpoint | Direction | Type | MPS | Interval | Interface/Alt |",
        "|---|---|---|---:|---:|---|",
    ]
    for endpoint in profile.get("endpoints", []):
        lines.append(
            f"| `0x{h(endpoint.get('address'), 2)}` | {endpoint.get('direction', '?')} | "
            f"{endpoint.get('type', '?')} | {endpoint.get('mps', '?')} | "
            f"{endpoint.get('interval', '?')} | "
            f"{endpoint.get('interface', '?')}/{endpoint.get('alt', '?')} |"
        )
    if not profile.get("endpoints"):
        lines.append("| — | — | No structured endpoint records | — | — | — |")

    lines += [
        "",
        "## Packet summary",
        "",
        f"- First-byte distribution: `{json.dumps(analysis['packet_first_bytes'], sort_keys=True)}`",
        f"- Length distribution: `{json.dumps(analysis['packet_lengths'], sort_keys=True)}`",
        f"- Probe-status records: `{json.dumps(profile.get('probe_status', []), sort_keys=True)}`",
        "",
        "## Firmware and notes",
        "",
        f"- Left probe: `{left.get('build', 'missing')}`, schema `{left.get('schema', '?')}`, probe `{left.get('probe', '?')}`",
        f"- Right probe: `{right.get('build', 'missing')}`, schema `{right.get('schema', '?')}`, probe `{right.get('probe', '?')}`",
        f"- User notes: {metadata.get('notes', '') or 'None'}",
        "",
        "## Attachments and privacy",
        "",
        "The accompanying **developer bundle** contains the complete raw serial trace, exact descriptors, and endpoint/control bytes needed for firmware analysis. It may also contain a controller serial number or authentication exchange; send it privately and do not post it publicly.",
        "",
    ]
    return "\n".join(lines)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def zip_paths(zip_path: Path, base: Path, paths: Iterable[Path]) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as archive:
        for path in paths:
            if path.is_dir():
                for child in sorted(path.rglob("*")):
                    if child.is_file():
                        archive.write(child, child.relative_to(base))
            elif path.is_file():
                archive.write(path, path.relative_to(base))


def write_outputs(lines: list[str], profile: dict[str, Any], output_root: Path,
                  session_name: str) -> dict[str, Path]:
    # Recipients only need to send one file, so the session folder ends up
    # holding just the ZIP; the loose files exist only while it is built.
    session = output_root / session_name
    session.mkdir(parents=True, exist_ok=False)
    staging = session / "_staging"
    staging.mkdir()
    raw_log = staging / "raw_serial.log"
    raw_log.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    full_json = staging / "controller_profile_full.json"
    report_md = staging / "REPORT.md"
    write_json(full_json, profile)
    report_md.write_text(make_markdown_report(profile), encoding="utf-8")

    descriptor_dir = staging / "descriptors"
    descriptor_dir.mkdir()
    for blob in profile.get("descriptor_blobs", []):
        if blob.get("record") != "desc" or not blob.get("complete"):
            continue
        try:
            data = bytes.fromhex(blob.get("hex", ""))
        except ValueError:
            continue
        name = f"g{blob.get('generation', 0)}_{slugify(str(blob.get('kind')))}_{slugify(str(blob.get('id')))}.bin"
        (descriptor_dir / name).write_bytes(data)

    oso_zip = session / f"SEND_TO_OSO_CUTE_{session_name}.zip"
    zip_paths(oso_zip, staging, [raw_log, full_json, report_md, descriptor_dir])
    shutil.rmtree(staging)
    return {
        "session": session,
        "oso_zip": oso_zip,
    }


class SerialCapture:
    def __init__(self, port: str) -> None:
        if serial is None:
            raise RuntimeError("pyserial is required: python -m pip install pyserial")
        self.serial = serial.Serial(port, KM_BAUD, timeout=0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._lock = threading.Lock()
        self.lines: list[str] = []
        self.plain_lines: list[str] = []
        self._buffer = b""

    def start(self, *, preserve_initial_data: bool = False) -> None:
        time.sleep(0.25)
        if not preserve_initial_data:
            self.serial.reset_input_buffer()
        self._thread.start()

    def _append(self, text: str) -> None:
        stamped = f"{utc_now()}\t{text}"
        with self._lock:
            self.lines.append(stamped)
            self.plain_lines.append(text)

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self.serial.read(self.serial.in_waiting or 1)
            except Exception as exc:  # preserve the failure in the report
                self._append(f"# SERIAL_ERROR {exc!r}")
                return
            if not chunk:
                continue
            self._buffer += chunk
            while b"\n" in self._buffer:
                raw, self._buffer = self._buffer.split(b"\n", 1)
                self._append(raw.decode("ascii", "replace").rstrip("\r"))

    def send(self, command: str) -> None:
        self.serial.write((command.rstrip() + "\n").encode("ascii"))
        self.serial.flush()
        self.mark(f"COMMAND {command}")

    def mark(self, text: str) -> None:
        self._append(f"# {text}")

    def snapshot_plain(self) -> list[str]:
        with self._lock:
            return list(self.plain_lines)

    def snapshot_stamped(self) -> list[str]:
        with self._lock:
            return list(self.lines)

    def wait_for(self, predicate: Callable[[str], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(predicate(line) for line in self.snapshot_plain()):
                return True
            time.sleep(0.05)
        return False

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        if self._buffer:
            self._append(self._buffer.decode("ascii", "replace"))
            self._buffer = b""
        self.serial.close()


def find_ch343_port(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    if serial is None:
        return None
    matches = [p.device for p in serial.tools.list_ports.comports()
               if p.vid == CH343_VID]
    return matches[0] if matches else None


def open_serial_capture(args: argparse.Namespace, *, wait_for_port: bool = False,
                        preserve_initial_data: bool = False) -> tuple[SerialCapture, str]:
    """Open the CH343 port, optionally waiting for it to enumerate.

    The CH343 shows up as soon as USB2 reaches the PC — the board itself
    can still be dark (it powers from USB1). An open port with zero bytes
    is therefore normal before USB1 is connected.
    """
    deadline = time.monotonic() + (30.0 if wait_for_port else 0.0)
    last_error: Exception | None = None
    next_status = time.monotonic()
    if wait_for_port:
        print("Waiting for the CH343 COM port (USB2 → PC)...", end="", flush=True)
    while True:
        port = find_ch343_port(args.port)
        if port:
            try:
                capture = SerialCapture(port)
                capture.start(preserve_initial_data=preserve_initial_data)
                return capture, port
            except Exception as exc:
                last_error = exc
        if not wait_for_port or time.monotonic() >= deadline:
            if wait_for_port:
                print(" not available")
            detail = f" ({last_error})" if last_error else ""
            raise RuntimeError(
                "Could not open the CH343 middle port. Check the USB2 (middle) "
                "cable to this PC" + detail
            )
        if time.monotonic() >= next_status:
            print(".", end="", flush=True)
            next_status = time.monotonic() + 1.0
        time.sleep(0.25)


def start_probe_collection(capture: SerialCapture, args: argparse.Namespace,
                           *, wait_for_power: bool = False) -> None:
    """Confirm the powered probe pair, then enable its structured telemetry."""
    ready = False
    if wait_for_power:
        print("Waiting for USB1 power and the Left probe...", end="", flush=True)
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            capture.send("km.version()")
            if capture.wait_for(lambda x: "kmbox:" in x, 0.75):
                ready = True
                break
            print(".", end="", flush=True)
        print(" ready" if ready else " not found")
    else:
        capture.send("km.version()")
        ready = capture.wait_for(lambda x: "kmbox:" in x, 2.5)
    if not ready:
        raise RuntimeError(
            "The CH343 port opened, but Left did not answer km.version(). "
            "Verify that USB1 is connected to a powered host."
        )
    capture.send("km.probe()")
    left_ok = capture.wait_for(
        lambda x: "[PRB] HELLO" in x and "side=L" in x and "probe=1" in x,
        3.0,
    )
    right_ok = capture.wait_for(
        lambda x: "[PRB] HELLO" in x and "side=R" in x and "probe=1" in x,
        3.0,
    )
    if not left_ok and not args.allow_legacy:
        raise RuntimeError("LEFT_PROBE is not installed (no probe=1 Left banner).")
    if not right_ok and not args.allow_legacy:
        raise RuntimeError("RIGHT_PROBE is not installed or IPC is unavailable.")
    capture.send("km.telem(1)")


def wait_with_message(seconds: float, message: str) -> None:
    print(message, end="", flush=True)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        print(".", end="", flush=True)
    print(" done")


def connection_countdown(message: str, seconds: int = 3) -> None:
    """Give the user an unambiguous, already-recording connection point."""
    print(message)
    for remaining in range(seconds, 0, -1):
        print(f"  {remaining}...", flush=True)
        time.sleep(1)
    print("  Connect now.")


def print_setup(title: str, lines: Iterable[str]) -> None:
    width = 72
    print("\n" + "=" * width)
    print(title.center(width))
    print("=" * width)
    for line in lines:
        print(line)
    print("=" * width)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        value = ""
    return value or default


ACTION_STEPS = [
    "Leave the controller completely idle for two seconds",
    "Move the LEFT stick in a full circle, then release it",
    "Move the RIGHT stick in a full circle, then release it",
    "Pull LT and RT separately, then release them",
    "Press A (Cross/X), B (Circle/O), X (Square), and Y (Triangle) separately",
    "Press D-pad directions, LB, RB, Menu, and View separately, one at a time",
]


# Handshake milestones detected live from probe/diagnostic lines. Order
# mirrors the passthrough pipeline; `critical` milestones gate a phase
# checkpoint, the rest are informational. host_out is the decisive one:
# zero host OUT packets after mount is the classic silent-failure stage.
PHASE_MILESTONES = [
    ("left_banner",     "Left probe banner",      lambda l: "[PRB] HELLO" in l and "side=L" in l),
    ("right_banner",    "Right probe banner",     lambda l: "[PRB] HELLO" in l and "side=R" in l),
    ("device_seen",     "Controller detected",    lambda l: "event=new_device" in l or "event=device_open" in l),
    ("device_ready",    "Descriptors relayed",    lambda l: "event=device_ready" in l),
    ("target_mount",    "Host mounted mirror",    lambda l: "event=target_mounted" in l),
    ("announce_cached", "GIP announce cached",    lambda l: "event=announce_cached" in l or "ANNOUNCE_CACHED" in l),
    ("announce_replay", "Announce replayed",      lambda l: "event=announce_replay" in l or "ANNOUNCE_REPLAY" in l),
    ("host_out",        "Host OUT traffic",       lambda l: "[L][EP] OUT" in l),
]
CRITICAL_MILESTONES = {"device_seen", "device_ready"}


def _stamp_of(stamped_line: str) -> str:
    return stamped_line.split("\t", 1)[0] if "\t" in stamped_line else ""


def _delta_ms(iso_a: str, iso_b: str) -> int | None:
    try:
        ta = datetime.fromisoformat(iso_a)
        tb = datetime.fromisoformat(iso_b)
        return int((tb - ta).total_seconds() * 1000)
    except ValueError:
        return None


def phase_checkpoint(capture: SerialCapture, start_index: int,
                     phase_name: str) -> dict[str, Any]:
    """Scan lines captured since start_index and print a milestone table.

    Returns a summary dict (also stored in the profile metadata) with the
    first timestamp of each milestone, the mount-vs-announce ordering, and
    a coarse failure-stage classification.
    """
    stamped = capture.snapshot_stamped()[start_index:]
    found: dict[str, str] = {}
    counts: dict[str, int] = Counter()
    for line in stamped:
        text = line.split("\t", 1)[1] if "\t" in line else line
        for key, _label, match in PHASE_MILESTONES:
            if match(text):
                counts[key] += 1
                found.setdefault(key, _stamp_of(line))

    print(f"\n--- {phase_name}: capture checkpoint ---")
    for key, label, _match in PHASE_MILESTONES:
        mark = "OK " if key in found else "-- "
        extra = f" x{counts[key]}" if counts.get(key, 0) > 1 else ""
        print(f"  [{mark.strip():>2}] {label}{extra}")

    stage = "complete"
    if "device_seen" not in found:
        stage = "no_physical_enumeration"
    elif "device_ready" not in found:
        stage = "descriptor_relay_failed"
    elif "target_mount" not in found:
        stage = "host_never_configured"
    elif "host_out" not in found:
        stage = "mounted_but_no_host_out"

    mount_vs_announce = None
    if "target_mount" in found and "announce_cached" in found:
        delta = _delta_ms(found["announce_cached"], found["target_mount"])
        if delta is not None:
            mount_vs_announce = delta
            if delta < 0 and "announce_replay" not in found:
                print("  WARNING: host mounted BEFORE the announce was cached and")
                print("  no replay was seen — replay-race signature (Case C).")
    if stage == "mounted_but_no_host_out":
        print("  NOTE: host configured the mirror but never sent OUT traffic.")
        print("  This capture is the most valuable kind — keep it.")

    return {
        "phase": phase_name,
        "stage": stage,
        "milestones": {k: found.get(k) for k, _l, _m in PHASE_MILESTONES},
        "counts": dict(counts),
        "announce_to_mount_ms": mount_vs_announce,
    }


def checkpoint_gate(summary: dict[str, Any]) -> str:
    """Ask how to proceed when a phase misses critical milestones."""
    missing_critical = [k for k in CRITICAL_MILESTONES
                        if not summary["milestones"].get(k)]
    if not missing_critical:
        return "continue"
    print(f"  Missing critical milestone(s): {', '.join(missing_critical)}")
    while True:
        answer = ask("  [r]etry this phase, [c]ontinue anyway, or [a]bort", "r").lower()
        if answer in ("r", "c", "a"):
            return {"r": "retry", "c": "continue", "a": "abort"}[answer]


def run_live(args: argparse.Namespace) -> tuple[list[str], dict[str, Any]]:
    print_setup("CONTROLLER PROBE — 3 PHASES", [
        "Phase 1  Cold start        — USB1 host connection is recorded last.",
        "Phase 2  Controller replug — controller unplugged/reconnected while",
        "                             USB1 stays connected.",
        "Phase 3  Input mapping     — guided stick/button presses.",
        "",
        "After each phase the tool verifies what it actually captured and",
        "offers a retry, so a cable fumble never wastes the whole session.",
    ])

    # Cables first, questions second: the serial port only needs USB2, so
    # open it and start recording before anything else happens.
    print_setup("SETUP — CONNECT THE RECORDING PORT", [
        "[CONNECT NOW]",
        "1. USB2 (middle) → this PC          (makes the COM port appear)",
        "2. USB3 (right)  → controller",
        "",
        "[LEAVE DISCONNECTED]",
        "3. USB1 (left)   → console/main PC  (the board stays dark until",
        "                    USB1 is connected — that is normal)",
    ])
    input("When USB2 and USB3 are connected, press Enter...")
    capture, port = open_serial_capture(
        args, wait_for_port=True, preserve_initial_data=True)
    print(f"\nRecording is LIVE on {port} at {KM_BAUD:,} baud.")

    # Metadata interview happens while the port idles — dead time anyway.
    metadata = {
        "controller_name": args.controller_name or ask(
            "What controller is connected to USB3?") or "unknown_controller",
        "model": args.model or ask(
            "Model or rear-label text (optional; press Enter to skip)") or "unknown",
        "host_device": ask(
            "What will USB1 be connected to? (PC or which console)") or "unknown",
        "capture_sequence": "cold start, controller replug, input mapping",
        "connection_mode": args.connection_mode or "direct USB3 connection during probe",
        "notes": args.notes or ask(
            "Anything unusual to note? (optional; press Enter to skip)", ""),
        "computer": os.environ.get("COMPUTERNAME", "unknown"),
        "serial_port": port,
    }
    phase_summaries: list[dict[str, Any]] = []

    try:
        capture.mark("SESSION_START")

        # ---- Phase 1: cold start (retryable) --------------------------------
        while True:
            input("Ready to connect USB1 and press the power button on the "
                  "controller? Press Enter...")
            phase_start = len(capture.snapshot_stamped())
            capture.mark("PHASE_1_COLD_START usb1_connect_now")
            connection_countdown(
                "Phase 1 armed and recording. Connect USB1 to the console/main "
                "PC when told.")
            print("If the controller has not lit up a few seconds after USB1 is\n"
                  "connected, press its power (Xbox/home) button once.")
            start_probe_collection(capture, args, wait_for_power=True)
            # The boot banner and enumeration arrive within a few seconds of
            # USB1 power; wait for device_ready instead of sleeping blind,
            # then allow the host handshake to play out.
            capture.wait_for(lambda x: "event=device_ready" in x,
                             15.0 if not args.quick else 6.0)
            wait_with_message(10.0 if not args.quick else 4.0,
                              "Capturing host enumeration/handshake")
            summary = phase_checkpoint(capture, phase_start, "Phase 1 cold start")
            action = checkpoint_gate(summary)
            if action == "retry":
                print("Retrying Phase 1: unplug USB1, wait two seconds.")
                input("When USB1 is unplugged, press Enter...")
                capture.mark("PHASE_1_RETRY usb1_disconnected")
                continue
            phase_summaries.append(summary)
            if action == "abort":
                raise KeyboardInterrupt("aborted at Phase 1 checkpoint")
            break

        # ---- Phase 2: controller replug (retryable) --------------------------
        if not args.quick:
            while True:
                print_setup("PHASE 2 — CONTROLLER REPLUG", [
                    "Keep USB1 and USB2 connected.",
                    "Unplug the controller from USB3; the countdown tells you",
                    "when to reconnect it. Don't forget to hit the power",
                    "button once after reconnecting. Recording never stops.",
                ])
                input("Unplug the controller from USB3, then press Enter...")
                phase_start = len(capture.snapshot_stamped())
                capture.mark("PHASE_2_REPLUG controller_disconnected")
                wait_with_message(2.0, "Recording disconnected state")
                connection_countdown(
                    "Recording is active. Reconnect the controller to USB3 when told.")
                print("If the controller stays dark after reconnecting, press its\n"
                      "power (Xbox/home) button once.")
                capture.mark("PHASE_2_REPLUG controller_connect_now")
                capture.wait_for(lambda x: "event=device_ready" in x, 15.0)
                wait_with_message(10.0, "Capturing replug handshake")
                summary = phase_checkpoint(capture, phase_start,
                                           "Phase 2 controller replug")
                action = checkpoint_gate(summary)
                if action == "retry":
                    continue
                phase_summaries.append(summary)
                if action == "abort":
                    raise KeyboardInterrupt("aborted at Phase 2 checkpoint")
                break

        # ---- Phase 3: input mapping ------------------------------------------
        if not args.no_actions:
            print("\nPHASE 3 — INPUT MAPPING")
            for index, instruction in enumerate(ACTION_STEPS, start=1):
                capture.mark(f"ACTION_BEGIN {index} {instruction}")
                input(f"{index}/{len(ACTION_STEPS)}: {instruction}. Press Enter when finished...")
                capture.mark(f"ACTION_END {index}")
                time.sleep(0.5)
        wait_with_message(3.0, "Capturing final idle state")
        capture.mark("SESSION_COMPLETE")
        capture.send("km.telem(0)")
        time.sleep(0.3)
    finally:
        try:
            capture.send("km.telem(0)")
            time.sleep(0.1)
        except Exception:
            pass
        capture.stop()

    metadata["phase_checkpoints"] = phase_summaries
    parser = ProbeParser()
    for line in capture.snapshot_plain():
        parser.process_line(line)
    return capture.snapshot_stamped(), parser.build_profile(metadata)


def analyze_file(path: Path, metadata: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    parser = ProbeParser()
    for line in lines:
        parser.process_line(line)
    return lines, parser.build_profile(metadata)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect and package a MAKCU controller compatibility report."
    )
    parser.add_argument("--port", help="CH343 command port (default: auto-detect)")
    parser.add_argument("--output", type=Path,
                        default=Path.cwd() / "Controller_Probe_Reports")
    parser.add_argument("--controller-name")
    parser.add_argument("--model")
    parser.add_argument("--connection-mode")
    parser.add_argument("--notes")
    parser.add_argument("--quick", action="store_true",
                        help="shorten fixed waits for development")
    parser.add_argument("--no-actions", action="store_true",
                        help="skip guided button/stick report mapping")
    parser.add_argument("--allow-legacy", action="store_true",
                        help="continue without both probe=1 firmware banners")
    parser.add_argument("--analyze", type=Path,
                        help="analyze an existing raw log instead of opening hardware")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.analyze:
            metadata = {
                "controller_name": args.controller_name or "offline capture",
                "model": args.model or "unknown",
                "connection_mode": args.connection_mode or "unknown",
                "notes": args.notes or f"Re-analyzed from {args.analyze}",
            }
            lines, profile = analyze_file(args.analyze, metadata)
        else:
            lines, profile = run_live(args)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        controller = slugify(str(profile.get("metadata", {}).get(
            "controller_name", "controller")))
        session_name = f"{stamp}_{controller}"
        args.output.mkdir(parents=True, exist_ok=True)
        outputs = write_outputs(lines, profile, args.output, session_name)
    except KeyboardInterrupt:
        print("\nCapture canceled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    print("\n" + "=" * 72)
    print("DONE")
    print(profile["analysis"]["verdict"])
    print(f"Please send this ZIP to oso_cute:\n{outputs['oso_zip']}")
    print("=" * 72)
    time.sleep(2)
    print("Opening the folder with the ZIP to share...")
    try:
        os.startfile(str(outputs["session"]))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
