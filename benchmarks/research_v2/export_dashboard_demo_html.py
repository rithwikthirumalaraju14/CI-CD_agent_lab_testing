"""Export the labeled dashboard demo as one offline HTML file."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from buildtrace.web import create_app  # noqa: E402


STUDY = "generated-example/v2"
STUDY_ID = "exploratory-25-development-v2"
DEFAULT_OUTPUT = ROOT / "deliverables" / f"{STUDY_ID}.html"
STATIC = ROOT / "src" / "buildtrace" / "static"
DATA = ROOT / "data" / "generated-example" / "experiments" / "v2"


def export(output: Path = DEFAULT_OUTPUT) -> Path:
    app = create_app({"TESTING": True, "DATA_DIR": ROOT / "data"})
    client = app.test_client()
    url = f"/dashboard?study={STUDY}"
    page = client.get(url)
    dashboard = client.get(f"/api/dashboard?study={STUDY}").get_json()
    if page.status_code != 200 or dashboard["data_origin"] != "generated-example" or dashboard["completed_trials"] != 225:
        raise ValueError("Expected the complete, labeled 225-trial demo study")
    html = page.get_data(as_text=True)

    for asset in ("app.css", "dashboard.css"):
        tag = f'<link rel="stylesheet" href="/static/{asset}">'
        if tag not in html:
            raise ValueError(f"Missing stylesheet link: {asset}")
        html = html.replace(tag, f"<style>\n{(STATIC / asset).read_text(encoding='utf-8')}\n</style>", 1)
    html = html.replace('<script src="/static/app.js" defer></script>', "")
    tag = '<script src="/static/dashboard.js" defer></script>'
    if tag not in html:
        raise ValueError("Missing dashboard script")
    html = html.replace(tag, f"<script>\n{(STATIC / 'dashboard.js').read_text(encoding='utf-8')}\n</script>", 1)

    # An exported file has one study; replace the server-only selector with its title.
    html, changed = re.subn(
        r'<form method="get" action="/dashboard" class="study-picker">.*?</form>',
        f'<div class="study-picker"><strong>{STUDY_ID}</strong></div>',
        html,
        count=1,
        flags=re.DOTALL,
    )
    if changed != 1:
        raise ValueError("Could not replace server-only study selector")
    html = html.replace('href="/"', 'href="#"', 1).replace('href="/dashboard"', 'href="#"', 1)

    downloads = {
        "csv": {"name": f"{STUDY_ID}-trials.csv", "type": "text/csv;charset=utf-8",
                "text": client.get(f"/dashboard.csv?study={STUDY}").get_data(as_text=True)},
        "json": {"name": f"{STUDY_ID}-dashboard.json", "type": "application/json;charset=utf-8",
                 "text": json.dumps(dashboard, ensure_ascii=False, indent=2)},
        "results": {"name": f"{STUDY_ID}-results.jsonl", "type": "application/x-ndjson;charset=utf-8",
                    "text": (DATA / "results.jsonl").read_text(encoding="utf-8")},
        "experiment": {"name": f"{STUDY_ID}-experiment.json", "type": "application/json;charset=utf-8",
                       "text": (DATA / "experiment.json").read_text(encoding="utf-8")},
    }
    links = {
        "csv": f"/dashboard.csv?study={STUDY}",
        "json": f"/api/dashboard?study={STUDY}",
        "results": f"/dashboard/download/results?study={STUDY}",
        "experiment": f"/dashboard/download/experiment?study={STUDY}",
    }
    for kind, href in links.items():
        tag = f'href="{href}"'
        if tag not in html:
            raise ValueError(f"Missing download link: {kind}")
        html = html.replace(tag, f'href="#" data-download="{kind}"', 1)

    payload = json.dumps(downloads, ensure_ascii=False).replace("<", "\\u003c")
    offline_script = f'''<script type="application/json" id="dashboard-downloads">{payload}</script>
<script>
document.addEventListener("DOMContentLoaded", () => {{
  const files = JSON.parse(document.getElementById("dashboard-downloads").textContent);
  document.querySelectorAll("[data-download]").forEach(link => {{
    link.addEventListener("click", event => {{
      event.preventDefault();
      const file = files[link.dataset.download];
      const url = URL.createObjectURL(new Blob([file.text], {{type: file.type}}));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = file.name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }});
  }});
}});
</script>'''
    html = html.replace("</body>", offline_script + "\n</body>", 1)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return output


def export_v1(output: Path = ROOT / "deliverables" / "exploratory-25-development-v1.html") -> Path:
    """Export the recorded exploratory v1 study as a self-contained HTML file."""
    study = "exploratory-25/dev-v1"
    study_id = "exploratory-25-development-v1"
    data_dir = ROOT / "data" / "exploratory-25" / "experiments" / "dev-v1"
    app = create_app({"TESTING": True, "DATA_DIR": ROOT / "data"})
    client = app.test_client()
    page = client.get(f"/dashboard?study={study}")
    dashboard = client.get(f"/api/dashboard?study={study}").get_json()
    if page.status_code != 200 or dashboard["classification"] != "exploratory" or dashboard["completed_trials"] != 225:
        raise ValueError("Expected the complete recorded v1 exploratory study")
    html = page.get_data(as_text=True)
    for asset in ("app.css", "dashboard.css"):
        tag = f'<link rel="stylesheet" href="/static/{asset}">'
        if tag not in html:
            raise ValueError(f"Missing stylesheet link: {asset}")
        html = html.replace(tag, f"<style>\n{(STATIC / asset).read_text(encoding='utf-8')}\n</style>", 1)
    html = html.replace('<script src="/static/app.js" defer></script>', "")
    tag = '<script src="/static/dashboard.js" defer></script>'
    html = html.replace(tag, f"<script>\n{(STATIC / 'dashboard.js').read_text(encoding='utf-8')}\n</script>", 1)
    html, changed = re.subn(
        r'<form method="get" action="/dashboard" class="study-picker">.*?</form>',
        f'<div class="study-picker"><strong>{study_id}</strong></div>', html, count=1, flags=re.DOTALL)
    if changed != 1:
        raise ValueError("Could not replace server-only study selector")
    html = html.replace('href="/"', 'href="#"', 1).replace('href="/dashboard"', 'href="#"', 1)

    downloads = {
        "csv": {"name": f"{study_id}-trials.csv", "type": "text/csv;charset=utf-8",
                "text": client.get(f"/dashboard.csv?study={study}").get_data(as_text=True)},
        "json": {"name": f"{study_id}-dashboard.json", "type": "application/json;charset=utf-8",
                 "text": json.dumps(dashboard, ensure_ascii=False, indent=2)},
        "results": {"name": f"{study_id}-results.jsonl", "type": "application/x-ndjson;charset=utf-8",
                    "text": (data_dir / "results.jsonl").read_text(encoding="utf-8")},
        "experiment": {"name": f"{study_id}-experiment.json", "type": "application/json;charset=utf-8",
                       "text": (data_dir / "experiment.json").read_text(encoding="utf-8")},
    }
    links = {
        "csv": f"/dashboard.csv?study={study}", "json": f"/api/dashboard?study={study}",
        "results": f"/dashboard/download/results?study={study}",
        "experiment": f"/dashboard/download/experiment?study={study}",
    }
    for kind, href in links.items():
        if f'href="{href}"' not in html:
            raise ValueError(f"Missing download link: {kind}")
        html = html.replace(f'href="{href}"', f'href="#" data-download="{kind}"', 1)
    payload = json.dumps(downloads, ensure_ascii=False).replace("<", "\\u003c")
    offline_script = f'''<script type="application/json" id="dashboard-downloads">{payload}</script>
<script>
document.addEventListener("DOMContentLoaded", () => {{
  const files = JSON.parse(document.getElementById("dashboard-downloads").textContent);
  document.querySelectorAll("[data-download]").forEach(link => {{
    link.addEventListener("click", event => {{
      event.preventDefault(); const file = files[link.dataset.download];
      const url = URL.createObjectURL(new Blob([file.text], {{type:file.type}}));
      const anchor = document.createElement("a"); anchor.href=url; anchor.download=file.name;
      document.body.appendChild(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }});
  }});
}});
</script>'''
    html = html.replace("</body>", offline_script + "\n</body>", 1)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output == DEFAULT_OUTPUT:
        print(export(args.output))
    else:
        print(export(args.output))
