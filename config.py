"""Storage location shared by every module; set ADS_STACK_DATA_DIR before importing."""

import os
from pathlib import Path

DATA_DIR = Path(os.path.abspath(os.path.expanduser(
    os.environ.get("ADS_STACK_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
)))
