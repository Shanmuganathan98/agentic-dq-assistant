import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "data" / "customer.csv").exists():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_dirty_customers.py")], check=True)
