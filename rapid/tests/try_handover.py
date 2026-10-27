"""
Try a handover point without a cup: both arms go to their handover poses so you can look.

    py try_handover.py 420 -50 390

x y z = where the LEFT fingertips hold the cup (same as HANDOVER in handover.py).
One arm moves at a time: left goes there first, then the right comes up under it.
"""
import sys

import handover as h

if len(sys.argv) != 4:
    sys.exit(__doc__)

hx, hy, hz = map(float, sys.argv[1:])
left = (hx, hy, hz + h.TOOL_L)                                       # left flange
rim_z = hz - h.GRIP_FROM_RIM
take = (hx, hy - h.CUP_RADIUS, rim_z + h.RIM_GRIP - h.TOOL_R)        # right flange
below = (take[0], take[1], take[2] - h.TAKE_APPROACH)
print(f"Left flange {left}, right below {below}, right take {take}")

h.acquire(None)
try:
    h.do("L", h.pt(left, h.DOWN))        # left to the handover, pointing down
    h.do("R", h.pt(below, h.UP))         # right under it, pointing up
    h.do("R", h.pt(take))                # right up to where it would grip
    input("Look at the handover. Enter = back out ")
    h.do("R", h.pt(below))               # right down first
  
except RuntimeError as e:
    sys.exit(f"Failed: {e}")