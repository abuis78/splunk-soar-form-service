"""Packaging checks: TAR layout (SOAR pitfalls) and README sections."""

import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {
    "soar_form_service/__init__.py",
    "soar_form_service/soar_form_service.json",
    "soar_form_service/soar_form_service_connector.py",
    "soar_form_service/soar_form_service_utils.py",
    "soar_form_service/soar_form_service.svg",
    "soar_form_service/soar_form_service_dark.svg",
    "soar_form_service/README.md",
}


class PackageTest(unittest.TestCase):
    def test_tar_layout(self):
        with tempfile.TemporaryDirectory() as out:
            subprocess.run([str(ROOT / "scripts/package.sh"), out], check=True, capture_output=True)
            (tar_path,) = Path(out).glob("soar_form_service_v*.tar")
            with tarfile.open(tar_path) as tar:  # plain tar, not gzip
                self.assertEqual(tar_path.suffix, ".tar")
                names = {m.name.rstrip("/") for m in tar.getmembers() if m.isfile()}
        self.assertEqual(names, EXPECTED)

    def test_readme_has_ten_sections(self):
        headings = re.findall(r"^## (\d+)\. ", (ROOT / "README.md").read_text(encoding="utf-8"), re.M)
        self.assertEqual(headings, [str(i) for i in range(1, 11)])


if __name__ == "__main__":
    unittest.main()
