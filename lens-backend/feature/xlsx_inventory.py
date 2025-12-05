from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parents[1] / "inventory_site"
if BASE_DIR.exists():
    sys.path.insert(0, str(BASE_DIR))

from inventory.services.aws_inventory import main


if __name__ == "__main__":
    main()
