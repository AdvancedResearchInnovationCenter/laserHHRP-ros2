import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))              # harness
sys.path.insert(0, str(HERE.parent))       # laser_hhrp_ui from the source tree
