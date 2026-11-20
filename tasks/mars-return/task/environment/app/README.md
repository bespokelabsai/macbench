# Mars sample return, end to end, on a budget

This repo is the team's workspace. The verifier reads two things from `main`:

- `design/**/*.sysml`: the whole vehicle in SysML v2, from the first stage on the pad to the return capsule. Start from `design/vehicle.sysml`.
- `flight/guidance.py` (plus any modules it imports from `flight/`): the flight software that flies it.

`program/test_results.json` holds the R&D test results the team has saved. The verifier uses the program office's own ledger instead.

Start with `docs/REFERENCE.md`. The domain library is in `/opt/designlang/library/`. `examples/` holds a small launcher in SysML and an open-loop ascent script that show the language and the interface. They do not get anywhere near Mars.

    design-check                     # check design/**/*.sysml: syntax, names, units, designations, joints

The program office is on the board: `program_info`, `program_status`, `design_review`, `cost_report`, `quote_test`, `order_test`, `test_result` and `cancel_test`. It reads the model from the team repository, so push before you call it.
