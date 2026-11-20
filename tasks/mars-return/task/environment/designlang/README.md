# designlang

The design language of mars-return: a vehicle described in SysML v2, checked by a public checker and compiled by a hidden compiler into the `vehicle.json` that `marsflight` reads.

In the team image (copied to `/opt/designlang`):
- `library/`: the domain library (`MarsReturnLibrary.sysml`) and extra units (`MarsReturnUnits.sysml`).
- `designlang/sysml.py`, `units.py`, `model.py`, `designations.py`, `walk.py`, `check.py`: the public checker, run as `design-check`. These files do not use the simulator.

On the hub only (they are not in the image; a hub plugin delivers them):
- `designlang/hidden/compile.py`: the compiler from SysML to the simulator's `vehicle.json`.
- `designlang/hidden/office.py`: the program office's link between a model and the simulator: design review, quotes, test targets by part path, the team's view of results, cost.
- `designlang/hidden/sanitize.py`: turns simulator messages into messages a team may read.

The example is `../app/examples/ascent_vehicle.sysml`.

Check a model with any Python 3.10 or newer, from the task folder:

```bash
PYTHONPATH=environment/designlang python3 -m designlang.check environment/app/examples/ascent_vehicle.sysml
```
