"""
Send ONE command to one arm and wait until it is done.

    py test_cmd.py L GRIP
    py test_cmd.py R OPEN
    py test_cmd.py L HOME
    py test_cmd.py L 400,80,400,180,0,0
"""
import sys

import handover as h

if len(sys.argv) != 3 or sys.argv[1] not in ("L", "R"):
    sys.exit(__doc__)

h.acquire(None)
try:
    h.do(sys.argv[1], sys.argv[2])
except RuntimeError as e:
    sys.exit(f"Failed: {e}")