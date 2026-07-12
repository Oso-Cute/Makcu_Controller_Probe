#!/usr/bin/env python3
"""MAKCU Controller Probe collector and report generator.

The live workflow talks to the CH343 middle port at 4 Mbaud, verifies that
LEFT_PROBE and RIGHT_PROBE are installed, guides a controller/Xbox capture,
then creates:

* a full developer bundle containing the raw trace and exact USB bytes; and
* a redacted public bundle with serial/authentication material removed.

An existing raw log can be re-analyzed with ``--analyze path.log`` without
hardware or pyserial.
"""

from __future__ import annotations

import argparse
import copy
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


TOOL_VERSION = "1.0.0"
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
            verdict = "Descriptors were captured, but Xbox never mounted the mirrored device."
            failure_stage = "xbox_enumeration"
        elif not out_packets:
            verdict = "Xbox mounted the device but no endpoint-OUT handshake traffic was captured."
            failure_stage = "startup_handshake"
        elif not gip_inputs:
            verdict = "Xbox sent OUT traffic, but no standard GIP 0x20 input report was captured."
            failure_stage = "authentication_or_report_format"
        else:
            verdict = "GIP announce, Xbox OUT traffic, and 0x20 input reports were captured."
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


def redacted_profile(profile: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(profile)
    for key in ("computer", "serial_port"):
        if key in result.get("metadata", {}):
            result["metadata"][key] = "[REDACTED]"
    serial_index = maybe_int(result.get("device", {}).get("serial_index", -1))
    for item in result.get("strings", []):
        if item.get("index") == serial_index:
            item["value"] = "[REDACTED]"
            item["hex"] = "[REDACTED]"
    for blob in result.get("descriptor_blobs", []):
        is_serial = (blob.get("kind") == "string" and
                     maybe_int(blob.get("id")) == serial_index)
        is_control = blob.get("record") == "control"
        if is_serial or is_control:
            blob["hex"] = "[REDACTED]"
    for packet in result.get("packets", []):
        packet["hex"] = "[REDACTED]"
    result["privacy"] = {
        "redacted": True,
        "removed": ["serial string bytes", "endpoint packet bytes",
                    "control-transfer data bytes"],
    }
    return result


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
        f"| Xbox target mounted | {'Yes' if analysis['target_mounted'] else 'No'} |",
        f"| Startup announce seen | {'Yes' if analysis['announce_seen'] else 'No'} |",
        f"| Announce replayed | {'Yes' if analysis['announce_replayed'] else 'No'} |",
        f"| Endpoint OUT packets | {analysis['out_packet_count']} |",
        f"| Standard GIP `0x20` inputs | {analysis['gip_input_count']} |",
        f"| IPC CRC failures | {analysis['crc_failure_count']} |",
        f"| Stale-control recoveries | {analysis['stale_timeout_count']} |",
        "",
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
        "The accompanying **developer bundle** contains the complete raw serial trace, exact descriptors, and endpoint/control bytes needed for firmware analysis. It may also contain a controller serial number or authentication exchange; email it privately.",
        "",
        "The separately generated **public-redacted bundle** removes those raw bytes and is safer to post publicly.",
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
    session = output_root / session_name
    session.mkdir(parents=True, exist_ok=False)
    raw_log = session / "raw_serial.log"
    raw_log.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    full_json = session / "controller_profile_full.json"
    redacted_json = session / "controller_profile_redacted.json"
    report_md = session / "EMAIL_REPORT.md"
    privacy = session / "PRIVACY_README.txt"
    write_json(full_json, profile)
    redacted = redacted_profile(profile)
    write_json(redacted_json, redacted)
    report_md.write_text(make_markdown_report(redacted), encoding="utf-8")
    privacy.write_text(
        "FULL DEVELOPER BUNDLE: private. May contain controller serial and "
        "authentication/control bytes.\n"
        "PUBLIC REDACTED BUNDLE: raw packet/control/serial bytes removed.\n",
        encoding="utf-8",
    )

    descriptor_dir = session / "descriptors"
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

    developer_zip = session / f"EMAIL_TO_DEVELOPER_{session_name}.zip"
    public_zip = session / f"PUBLIC_REDACTED_{session_name}.zip"
    zip_paths(developer_zip, session, [raw_log, full_json, redacted_json,
                                       report_md, privacy, descriptor_dir])
    zip_paths(public_zip, session, [redacted_json, report_md, privacy])
    return {
        "session": session,
        "report": report_md,
        "developer_zip": developer_zip,
        "public_zip": public_zip,
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

    def start(self) -> None:
        time.sleep(0.25)
        self.serial.reset_input_buffer()
        self._thread.start()

    def _append(self, text: str) -> None:
        stamped = f"{utc_now()}\t{text}"
        with self._lock:
            self.lines.append(stamped)
            self.plain_lines.append(text)
        if "[PRB]" in text and not any(x in text for x in (" BLOB ", " PKT ")):
            print("  ", text)

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


def wait_with_message(seconds: float, message: str) -> None:
    print(message, end="", flush=True)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        print(".", end="", flush=True)
    print(" done")


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
    "Press A, B, X, and Y separately",
    "Press D-pad directions, LB, RB, Menu, and View separately",
]


def run_live(args: argparse.Namespace) -> tuple[list[str], dict[str, Any]]:
    port = find_ch343_port(args.port)
    if not port:
        raise RuntimeError(
            "No CH343 middle port found. Connect USB2 to this PC or pass --port COMx."
        )
    metadata = {
        "controller_name": args.controller_name or ask("Controller name", "GameSir G7 Pro"),
        "model": args.model or ask("Exact variant/rear-label model", "unknown"),
        "connection_mode": args.connection_mode or ask(
            "Connection and selected mode", "direct USB cable / Xbox wired mode"),
        "notes": args.notes or ask("Optional notes", ""),
        "computer": os.environ.get("COMPUTERNAME", "unknown"),
        "serial_port": port,
    }

    print(f"\nOpening {port} at {KM_BAUD:,} baud...")
    capture = SerialCapture(port)
    capture.start()
    try:
        capture.mark("SESSION_START")
        capture.send("km.version()")
        if not capture.wait_for(lambda x: "kmbox:" in x, 2.5):
            raise RuntimeError(
                "The CH343 port opened, but Left did not answer km.version(). "
                "Verify board power and that USB2 is the middle port."
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

        print("\nCAPTURE 1 — physical controller enumeration")
        print("Keep the MIDDLE USB2 cable connected to this PC.")
        input("Unplug the controller from USB3 and unplug Xbox USB1, then press Enter...")
        capture.mark("STAGE prepare controller_unplugged xbox_unplugged")
        input("Now connect the controller directly to USB3 in Xbox/wired mode, then press Enter...")
        capture.mark("STAGE controller_connected")
        wait_with_message(7.0 if not args.quick else 3.0,
                          "Capturing controller descriptors")

        print("\nCAPTURE 2 — Xbox enumeration and handshake")
        input("Connect USB1 to the powered-on Xbox LAST, then press Enter...")
        capture.mark("STAGE xbox_connected_last")
        wait_with_message(14.0 if not args.quick else 6.0,
                          "Capturing Xbox enumeration/handshake")

        if not args.no_actions:
            print("\nCAPTURE 3 — report mapping")
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
    print(profile["analysis"]["verdict"])
    print(f"Report:           {outputs['report']}")
    print(f"Email privately:  {outputs['developer_zip']}")
    print(f"Safe to post:     {outputs['public_zip']}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
