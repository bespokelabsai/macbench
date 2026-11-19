# Black Start

Restore the national grid after a total blackout. Read [MANUAL.md](MANUAL.md) first.

- `areas/aNN/controller.py`: one controller per control area (starters that do nothing).
- `lib/`: shared code for the controllers (`import lib.x`).
- `grid/public_grid.json`: the grid map every control room has.
- `docs/sample_obs/`: real observations from a practice scenario.
- `tools/check_controller.py`: loads every controller and runs it on the samples.

Board tools: `grid_info`, `area_data`, `practice_run`, `practice_result`, `practice_status`.
