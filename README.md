# BuildTrace runner and UI source

This branch contains the Python runner and web UI for the CI/CD agent memory
experiment. The three agent modes share one solver loop and verifier. The UI
shows saved trials, selected insight packets, observable plans and tool calls,
WikiTrace source reads, patches, and CI outcomes.

The 50 learning inputs and the development/exam inputs live on separate Git
branches in this repository. Generated captures, agent trajectories, the frozen
memory corpus, and local credentials are not part of this source branch. The
older research benchmark modules required by the dashboard are included.

The [failed-five learning audit](docs/FAILED_FIVE_LEARNING_RESULT.md) records the
five repaired learning questions, all 24 diagnostic attempts, admission checks,
and model usage. The original 45 questions and frozen learning corpus were not
changed. Local scripts for reproducing the admission, merge, and report are in
`tools/research_failed_five/` and require the saved operator data.

## Run locally

Use Python 3.11 or newer. Create a virtual environment, then install the
project with `python -m pip install -e ".[dev]"`. Start the read-only UI with
`python -m buildtrace serve`. The optional question launch controls require
`python -m buildtrace serve --enable-question-runs`, configured credentials,
and local experiment data.

See `docs/EXPERIMENTS.md` and `docs/RESEARCH_PROTOCOL.md` for the runner and
research controls. The four-problem question UI is exploratory; held-out exam
results require the pinned protocol and verifier evidence.

## First-50 exploratory dashboard

`tools/first50_dashboard.py` is a separate, read-only operator view for the
five predeclared batches of ten problems. It reads the saved batch plan,
packets, results, and observable trajectories. For each attempt it shows
whether WikiTrace opened an original source excerpt, which insights were
selected, saved plan notes, CI links, tokens, estimated cost, Gain, and
Regression. It does not launch agents or expose private model reasoning.

Run it with the plan and result directories from your local experiment:

```powershell
python tools/first50_dashboard.py --plan-root path/to/exploratory-first50-v1 --run-root path/to/data/exploratory-first50/experiments --port 8777
```

Open `http://127.0.0.1:8777/` and refresh to read newly saved results. The
dashboard is local only; the plan and result files remain outside this source
branch. The batch comparison is exploratory and does not replace independent
review of whether historical evidence changed an adopted repair.
