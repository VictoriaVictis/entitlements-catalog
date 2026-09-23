import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "unlisted_catalog.py"
spec = importlib.util.spec_from_file_location("unlisted_catalog", SCRIPT)
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


class CatalogTests(unittest.TestCase):
    def test_catalog_keeps_only_unlisted_ids_and_curated_names(self):
        records = {
            "30": {"parent": 2, "name": "PICS name"},
            "10": {"parent": 1, "name": "visible"},
            "20": {"parent": 1, "name": "hidden"},
        }
        manual = {"2": {"dlcs": {"30": "Curated name", "40": "manual only"}}}
        self.assertEqual(catalog.build_catalog(records, manual, {10, 40}), {
            "1": {"dlcs": {"20": "hidden"}},
            "2": {"dlcs": {"30": "Curated name"}},
        })

    def test_scan_repeats_safely_and_wraps_only_at_ceiling(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            cursor = state / "cursor.json"
            records = state / "records.json"

            def fake_scanner(command, check):
                start, count, output = int(command[2]), int(command[3]), Path(command[4])
                output.write_text(json.dumps({
                    "start": start, "count": count,
                    "dlcs": [{"id": start, "parent": 100, "name": "hidden"}],
                }), encoding="utf-8")

            with patch.object(catalog, "CURSOR", cursor), patch.object(catalog, "RECORDS", records), \
                    patch.object(catalog.subprocess, "run", side_effect=fake_scanner):
                catalog.scan(2, 3, Path("scanner.dll"))
                self.assertEqual(json.loads(cursor.read_text())["next_app_id"], 3)
                catalog.scan(2, 3, Path("scanner.dll"))
                self.assertEqual(json.loads(cursor.read_text()), {
                    "next_app_id": 1, "completed_passes": 1,
                })
                self.assertEqual(set(json.loads(records.read_text())), {"1", "3"})


if __name__ == "__main__":
    unittest.main()
