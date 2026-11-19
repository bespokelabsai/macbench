"""Reference flight software for the mars-return task (Bespoke Labs).

It flies every phase in closed loop on the navigation estimates in obs. The simulator calls
reset(info) once and step(obs) at the end of every arc. gnc/flight.py picks the handler for the
current phase:
  gnc/earth.py        pad wait, S1 ascent, S2 insertion, parking orbit and the trans-Mars burn
  gnc/cruise.py       correction burns on both cruises and the approaches to Mars and Earth
  gnc/edl.py          Mars entry, parachute and powered descent to Jezero
  gnc/mars_ascent.py  surface stay, Mars ascent and the trans-Earth burn
  gnc/earth_entry.py  passive capsule entry at Utah

"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The simulator reads this process's stderr only after the process exits. Tudat writes warnings to
# stderr, and once the pipe is full (8 KB for a user with many pipes open, 64 KB otherwise) every write
# blocks, so the flight software stops answering. Send stderr to a file (MF_STDERR) or to /dev/null.
try:
    os.dup2(os.open(os.environ.get("MF_STDERR", os.devnull), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644), 2)
except OSError:
    pass

if os.environ.get("MF_TRACE"):
    # development aid: dump every thread's stack every MF_TRACE seconds to MF_TRACE_FILE
    import faulthandler
    faulthandler.dump_traceback_later(float(os.environ["MF_TRACE"]), repeat=True,
                                      file=open(os.environ.get("MF_TRACE_FILE", "/tmp/mf_trace.txt"), "w"))

from gnc.fault_protection import FaultProtection  # noqa: E402
from gnc.flight import Flight  # noqa: E402

_FL = None


def reset(info):
    global _FL
    # fault protection: switch to the spare avionics string, restart after an upset, isolate a leaking RCS
    _FL = FaultProtection(Flight(info))


def step(obs):
    return _FL.step(obs)
