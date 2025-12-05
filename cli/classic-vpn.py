from pathlib import Path
import runpy
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / "lens-backend" / "feature" / "classic_vpn.py"

if __name__ == "__main__":
    if not TARGET.exists():
        sys.stderr.write("classic_vpn.py not found alongside the backend feature modules.\n")
        sys.exit(1)
    runpy.run_path(str(TARGET), run_name="__main__")
