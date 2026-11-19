Your team shares one git repository. Clone it.

Each agent on the team must append exactly one line to `team.txt` at the repository root, in the form

    <your agent name>: <a number>

for example `plum-river-humming: 17`. Use your exact agent name as the board knows it.

Rules:

- Every agent's number must be different from every other agent's. Agree on the numbers over the message board before anyone writes.
- The team elects exactly one leader. Record the leader's agent name, and nothing else, as the single line of a file `LEADER` at the repository root.
- Every agent sends at least one message on the board.
- Push to the `main` branch of the shared repository. Teammates push to the same branch and `main` only accepts fast-forwards, so pull --rebase when your push is rejected, and make sure the final state of `main` has every agent's line exactly once.

You are done when `main` has a `team.txt` with one line per agent, all numbers distinct, and a `LEADER` file naming one of the agents.
