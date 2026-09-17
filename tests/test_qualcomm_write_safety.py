"""Hardware-free regression tests for the Firehose write boundary."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from huaxin.core import qualcomm


class WriteSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.image = Path(self.directory.name) / "boot.img"
        self.edl = Mock()
        self.ctx = Mock()
        session = SimpleNamespace(
            native=SimpleNamespace(ProgramRequest=SimpleNamespace,
                                   build_program_xml=lambda request: "program"),
            edl=self.edl, sector_size=lambda: 512,
        )
        self.session = patch.object(qualcomm, "_live", session)
        self.session.start()
        self.addCleanup(self.session.stop)

    def test_oversized_image_never_reaches_device(self):
        self.image.write_bytes(b"x" * 1024)
        with self.assertRaises(ValueError):
            qualcomm.flash_partition(self.ctx, self.image, 100, num_sectors=1)
        self.edl.program_partition.assert_not_called()

    def test_partial_sector_is_padded_without_overwriting_unused_capacity(self):
        self.image.write_bytes(b"x" * 513)
        written = qualcomm.flash_partition(self.ctx, self.image, 100, num_sectors=20)
        request, payload = self.edl.program_partition.call_args.args
        self.assertEqual(written, 513)
        self.assertEqual(request.num_partition_sectors, 2)
        self.assertEqual(request.start_sector, 100)
        self.assertEqual(payload, b"x" * 513 + b"\0" * 511)
        self.assertEqual(len(payload), request.num_partition_sectors * 512)

    def test_aligned_image_is_unchanged(self):
        self.image.write_bytes(b"x" * 1024)
        qualcomm.flash_partition(self.ctx, self.image, 100)
        request, payload = self.edl.program_partition.call_args.args
        self.assertEqual(request.num_partition_sectors, 2)
        self.assertEqual(payload, b"x" * 1024)

    def test_negative_range_is_rejected(self):
        self.image.write_bytes(b"x")
        for start, count in [(-1, 0), (0, -1)]:
            with self.assertRaises(ValueError):
                qualcomm.flash_partition(self.ctx, self.image, start, count)
        self.edl.program_partition.assert_not_called()


if __name__ == "__main__":
    unittest.main()
