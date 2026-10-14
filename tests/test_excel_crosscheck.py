"""The Excel workbook and the Python code must agree (needs LibreOffice)."""

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

pytestmark = pytest.mark.skipif(
    not (shutil.which("soffice") or shutil.which("libreoffice")),
    reason="LibreOffice is not installed",
)


def test_excel_workbook_matches_python():
    import excel_crosscheck

    assert excel_crosscheck.main(["--tolerance", "1e-9"]) == 0
