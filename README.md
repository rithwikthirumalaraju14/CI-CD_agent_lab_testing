# BuildTrace runner and UI source

This branch contains the Python runner and web UI for the CI/CD agent memory
experiment. The three agent modes share one solver loop and verifier. The UI
shows saved trials, selected insight packets, observable plans and tool calls,
WikiTrace source reads, patches, and CI outcomes.

The 50 learning inputs and the development/exam inputs live on separate Git
branches in this repository. Generated captures, agent trajectories, the frozen
memory corpus, and local credentials are not part of this source branch. The
older research benchmark modules required by the dashboard are included.

## Run locally

Use Python 3.11 or newer. Create a virtual environment, then install the
project with `python -m pip install -e ".[dev]"`. Start the read-only UI with
`python -m buildtrace serve`. The optional question launch controls require
`python -m buildtrace serve --enable-question-runs`, configured credentials,
and local experiment data.

See `docs/EXPERIMENTS.md` and `docs/RESEARCH_PROTOCOL.md` for the runner and
research controls. The four-problem question UI is exploratory; held-out exam
results require the pinned protocol and verifier evidence.
