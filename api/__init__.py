"""wrench-board backend package."""

import sys

# Bridge .env → os.environ at the earliest possible point, so board parsers that
# read os.environ at import time (notably the XZZ engine's DES key, and the FZ
# cipher's KEY_WORDS) see the .env-configured value. pydantic-settings populates
# the Settings object but not os.environ — this fills that gap.
#
# Skipped under pytest: the suite must stay hermetic w.r.t. the developer's local
# .env (tests drive these keys explicitly via monkeypatch), and parser modules
# freeze their key at import time, so a .env-loaded value would otherwise leak in.
if "pytest" not in sys.modules:
    from api.env_bootstrap import load_env_file

    load_env_file()

__version__ = "0.1.0"
