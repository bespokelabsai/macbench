# Beat Petra at 0 A.D.

Write a game AI for 0 A.D. Release 28, the open-source historical real-time strategy game, that wins conquest matches against Petra, the game's built-in AI. Petra's difficulty levels are handicaps: at Medium it plays with the same economy as you, at Hard and Very Hard it gathers 25% and 56% faster, and the levels above Very Hard gather up to six times as fast; the strongest, the four Elite levels, also build and train faster and field stronger units and structures.

The grader plays 120 hidden 1v1 matches against Petra, from Very Easy to beyond Very Hard and with each of its personalities (defensive, balanced, aggressive), on maps, seeds and civilizations you have not seen. Your score is the playing strength those results imply, on a line from playing like Petra Very Easy (0) to playing like the strongest opponent, Elite 4 (1.0); at 1.0 you would take about 94% of the points on the hidden matches, and there is no cap above. The full rules are in `/app/MATCH.md`.

Your AI lives in `/app/legion/`, and `main.js` must export `LegionBot`. A starter bot that only gathers is already there. Describe your AI in `/app/AI.md`. The match code the grader uses is in `/app/harness/`. All game scripts except Petra are readable in `/app/reference/`. Petra is not installed in your sandbox: practice matches against Petra are played on the team's hub with the board tools `practice_info`, `practice_play`, `practice_result` and `practice_status`, from one pool of 30 matches shared by the whole team for the whole run (see `/app/MATCH.md`, Practice).

You have 4 hours.

The team's `main` (the AI in `legion/` and `AI.md`, as pushed) is graded at minute 30, hour 1, hour 2, hour 3 and hour 4 of the run, in the same way each time. Your score is the grade at hour 4, when the run ends. The earlier grades are only recorded, to see how the work progressed, and the team does not see them. So keep `main` valid and ready to submit at all times, and push each improvement as soon as it is ready. Keep improving your AI until the time runs out.

No internet. Write the AI yourself: do not copy or adapt Petra or any other existing AI. Act only by issuing commands for your own player; every match you do not lose is re-played from your logged commands, and one that does not reproduce counts as a loss.
Work autonomously, do not ask user for input.
