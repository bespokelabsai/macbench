# Multi Agent Coordination Bench

This repository holds the tasks of [**Multi Agent Coordination Bench**](https://benchmarks.bespokelabs.ai/multi-agent-coordination-bench/)
(MAC Bench), a benchmark of twelve natively multi-agent, long-horizon tasks that measures how well teams of frontier
model agents coordinate.

<p align="center">
  <img src="assets/mac-bench.gif" alt="Agents on platforms over water post notes to one shared message board, and messages travel from the board to their teammates." width="900">
</p>

Each task is built so that the work of one agent depends on the work of its teammates. Every task runs in three
settings: a single agent, a team of k agents (2, 4 or 8) that coordinate over a shared message board, and k agents
without coordination. Comparing them separates the gain from adding more agents from the gain from coordination.
The [benchmark page](https://benchmarks.bespokelabs.ai/multi-agent-coordination-bench/) has the full report, the
leaderboard and the results for every task.

## Tasks

| Task | Domain | Type | Folder |
|---|---|---|---|
| Cave Rescue | emergency response | Shared | [`tasks/caverescue`](tasks/caverescue) |
| Mars Return | space mission design | Shared | coming soon |
| Holdout-game | survival game | Distributed | coming soon |
| 0 A.D. | game AI | Shared | coming soon |
| Power Outage | power grid restoration | Distributed | coming soon |
| Seed to Profit | company management | Shared | coming soon |
| Mindustry | factory and defense game | Distributed | coming soon |
| Dubai ATC | air traffic control | Shared | coming soon |
| Chicago Skyline Survey | drone survey planning | Shared | coming soon |
| Spectrum to Molecule | mass spectrometry | Shared | coming soon |
| RNA Secondary Structure | computational biology | Shared | coming soon |
| SOTA Compressor | algorithm engineering | Shared | coming soon |

In a **shared** task all agents start with the same information and actions. In a **distributed** task the
environment gives each agent its own part of the system to see or control.

## Running a task

Each task is a [Harbor](https://github.com/harbor-framework/harbor) task folder. It runs with plain Harbor for a
single agent, and with [multi-agent-harness](https://github.com/bespokelabsai/multi-agent-harness) for a team. The
README in each task's folder has the setup and the exact commands.

## Citation

```bibtex
@article{bespokelabs2026macbench,
  author  = {Shreyas Pimpalgaonkar and Kartik Sharma and Harsh Agarwal and Daniel Wang and Prajval Nakrani and Shivank Garg and Shahryar Adil Khan and Hardik Khichi and Anirudha Ramesh and Vishnu Suresh and Robert Zhang and Siddhant Jain and Alex Dimakis and Carlos Bonilla and Mahesh Sathiamoorthy},
  title   = {Multi Agent Coordination Bench},
  journal = {Bespoke Labs Blog},
  year    = {2026},
  note    = {https://benchmarks.bespokelabs.ai/multi-agent-coordination-bench/},
}
```
