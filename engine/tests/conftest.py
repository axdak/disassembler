import sys
from pathlib import Path

# Add scripts directory to sys.path so tests can import modules from it
project_root = Path(__file__).resolve().parent.parent
scripts_dir = project_root / "scripts"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
