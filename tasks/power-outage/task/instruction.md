Your team runs the sixteen control rooms of a national grid that has just gone completely dark. Write the restoration: a controller for every control area in `areas/<area>/controller.py` and the protocol between the areas, so that the country gets its power back as fast and as safely as possible. The grader plays your controllers, each area isolated in its own process and talking only through messages, on 48 hidden blackout scenarios.

You have 4 hours.

The team's `main` (the area controllers in `areas/` and the code they import, as pushed) is graded at minute 30, hour 1, hour 2, hour 3 and hour 4 of the run, in the same way each time. Your score is the grade at hour 4, when the run ends. The earlier grades are only recorded, to see how the work progressed, and the team does not see them. So keep `main` valid and ready to submit at all times, and push each improvement as soon as it is ready.

Everything you need is in `MANUAL.md` in the repository: the grid, what each control room knows, the controller interface, how the grid behaves, the score, the board tools for your seats (`grid_info`, `area_data`), the study simulator in `tools/study/` that you can run as often as you like, and drills on the true simulator (`practice_run`, `practice_result`, `practice_status`), which come from one pool of 40 full drills shared by the whole team for the whole run.

Work autonomously; do not ask the user for input.
