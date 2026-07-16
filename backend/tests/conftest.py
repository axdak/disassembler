import sys
from pathlib import Path

# 让 backend/ 下的模块可被 import
backend = Path(__file__).resolve().parent.parent
if str(backend) not in sys.path:
    sys.path.insert(0, str(backend))
