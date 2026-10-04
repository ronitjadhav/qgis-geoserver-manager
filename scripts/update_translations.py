#! python3  # noqa: E265

"""Extract every translatable string into the .ts files, keeping the translations.

    python scripts/update_translations.py

Every .ts in geoserver_manager/resources/i18n/ is updated in one go. Uses
pylupdate6 (pip install PyQt6), not pylupdate5: that one silently skipped a
translate() call black had wrapped onto several lines, or whose text was
written as adjacent literals: 65 of 455 strings when measured. CI runs this
before lrelease at packaging time.
"""

import subprocess
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "geoserver_manager"

ts_files = sorted((PLUGIN / "resources" / "i18n").glob("*.ts"))
ts_args = [arg for ts in ts_files for arg in ("--ts", str(ts))]
try:
    sys.exit(subprocess.call(["pylupdate6", "--no-obsolete", *ts_args, str(PLUGIN)]))
except FileNotFoundError:
    sys.exit("pylupdate6 not found. python -m pip install PyQt6")
