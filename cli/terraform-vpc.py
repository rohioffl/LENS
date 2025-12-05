from pathlib import Path
import runpy
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / "lens-backend" / "feature" / "terraform_vpc.py"

if __name__ == "__main__":
    if not TARGET.exists():
        sys.stderr.write("terraform_vpc.py not found next to this launcher.\n")
        sys.exit(1)
    runpy.run_path(str(TARGET), run_name="__main__")
