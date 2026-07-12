import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from controller_probe import (  # noqa: E402
    ProbeParser,
    make_markdown_report,
    redacted_profile,
    write_outputs,
)


DEVICE = bytes.fromhex("12010002ff47d0405e04120b110501020301")
SERIAL = bytes([20, 3]) + "SERIAL123".encode("utf-16-le")


def sample_lines():
    return [
        "[PRB] HELLO schema=1 side=L build=makcu-left-probe-1 probe=1 ipc_baud=2000000",
        "[R] [PRB] HELLO schema=1 side=R build=makcu-right-probe-1 probe=1 ipc_baud=2000000",
        "[R] [PRB] STATE side=R event=new_device address=1",
        "[R] [PRB] DEVICE vid=045e pid=0b12 bcd=0511 usb=0200 speed=full address=1 ep0=64 config_value=1 configs=1 class=ff subclass=47 protocol=d0 mfg_index=1 product_index=2 serial_index=3",
        f"[R] [PRB] BLOB record=desc kind=device id=0 off=0 total=18 hex={DEVICE[:9].hex()}",
        f"[R] [PRB] BLOB record=desc kind=device id=0 off=9 total=18 hex={DEVICE[9:].hex()}",
        f"[R] [PRB] BLOB record=desc kind=string id=3 off=0 total={len(SERIAL)} hex={SERIAL.hex()}",
        "[R] [PRB] CONFIG value=1 total=32 interfaces=1 attributes=a0 max_power=50 string_index=0",
        "[R] [PRB] IF number=0 alt=0 endpoints=2 class=ff subclass=47 protocol=d0 string_index=0",
        "[R] [PRB] EP interface=0 alt=0 address=82 direction=in attributes=03 type=interrupt mps=64 interval=4",
        "[R] [PRB] EP interface=0 alt=0 address=02 direction=out attributes=03 type=interrupt mps=64 interval=4",
        "[L][PRB] PROBE_STATUS seq=65532 status=0 ms_os_valid=1 qualifier_valid=0 other_speed_valid=0",
        "[R] [PRB] STATE side=R event=announce_cached ep=82 len=4",
        "[R] [PRB] PKT id=1 side=R dir=in phase=premount ep=82 status=0 t_us=100 len=4 captured=4 off=0 total=4 hex=02000100",
        "[R] [PRB] STATE side=R event=target_mounted announce_cached=1 announce_ep=82 announce_len=4",
        "[R] [PRB] STATE side=R event=announce_replay ep=82 len=4 sent=1",
        "[R] [PRB] PKT id=2 side=R dir=out phase=submit ep=02 status=0 t_us=200 len=5 captured=5 off=0 total=5 hex=1e30050100",
        "[R] [PRB] PKT id=3 side=R dir=in phase=postmount ep=82 status=0 t_us=300 len=20 captured=20 off=0 total=20 hex=2000001000000000000000000000000000000000",
    ]


class ProbeParserTests(unittest.TestCase):
    def parse(self):
        parser = ProbeParser()
        for line in sample_lines():
            parser.process_line(line)
        return parser.build_profile({
            "controller_name": "Test Controller",
            "computer": "PRIVATE-PC",
            "serial_port": "COM99",
        })

    def test_structured_capture_reassembles_and_classifies(self):
        profile = self.parse()
        self.assertEqual(profile["generation_analyzed"], 1)
        self.assertEqual(profile["device"]["vid"], "045e")
        self.assertEqual(profile["endpoints"][0]["address"], 0x82)
        device_blob = next(
            b for b in profile["descriptor_blobs"]
            if b["record"] == "desc" and b["kind"] == "device"
        )
        self.assertTrue(device_blob["complete"])
        self.assertEqual(device_blob["hex"], DEVICE.hex())
        self.assertTrue(profile["analysis"]["target_mounted"])
        self.assertEqual(profile["analysis"]["out_packet_count"], 1)
        self.assertEqual(profile["analysis"]["gip_input_count"], 1)
        self.assertEqual(profile["analysis"]["failure_stage"], "none_observed")

    def test_redaction_removes_serial_and_packet_bytes(self):
        profile = self.parse()
        redacted = redacted_profile(profile)
        serial = next(x for x in redacted["strings"] if x["index"] == 3)
        self.assertEqual(serial["value"], "[REDACTED]")
        self.assertEqual(redacted["metadata"]["computer"], "[REDACTED]")
        self.assertEqual(redacted["metadata"]["serial_port"], "[REDACTED]")
        self.assertTrue(all(p["hex"] == "[REDACTED]"
                            for p in redacted["packets"]))
        report = make_markdown_report(redacted)
        self.assertNotIn("SERIAL123", report)
        self.assertIn("GIP announce, Xbox OUT traffic", report)

    def test_output_writes_private_and_public_bundles(self):
        profile = self.parse()
        with tempfile.TemporaryDirectory() as td:
            outputs = write_outputs(sample_lines(), profile, Path(td), "session")
            self.assertTrue(outputs["developer_zip"].is_file())
            self.assertTrue(outputs["public_zip"].is_file())
            with zipfile.ZipFile(outputs["developer_zip"]) as zf:
                names = set(zf.namelist())
                self.assertIn("raw_serial.log", names)
                self.assertIn("controller_profile_full.json", names)
            with zipfile.ZipFile(outputs["public_zip"]) as zf:
                names = set(zf.namelist())
                self.assertNotIn("raw_serial.log", names)
                self.assertNotIn("controller_profile_full.json", names)

    def test_missing_mount_is_reported_as_enumeration_failure(self):
        parser = ProbeParser()
        for line in sample_lines()[:11]:
            parser.process_line(line)
        profile = parser.build_profile()
        self.assertEqual(profile["analysis"]["failure_stage"], "xbox_enumeration")


if __name__ == "__main__":
    unittest.main()
