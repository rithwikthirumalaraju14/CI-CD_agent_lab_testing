document.addEventListener("DOMContentLoaded", () => {
  const refresh = document.getElementById("dashboard-refresh");
  if (refresh) refresh.addEventListener("click", () => window.location.reload());

  const caseSearch = document.getElementById("case-search");
  const domainFilter = document.getElementById("domain-filter");
  const caseRows = [...document.querySelectorAll("#case-table tbody tr")];
  const filterCases = () => {
    const query = (caseSearch?.value || "").trim().toLowerCase();
    const domain = domainFilter?.value || "";
    for (const row of caseRows) {
      row.hidden = (query && !row.dataset.search.includes(query)) || (domain && row.dataset.domain !== domain);
    }
  };
  caseSearch?.addEventListener("input", filterCases);
  domainFilter?.addEventListener("change", filterCases);

  const modeFilter = document.getElementById("trial-mode");
  const outcomeFilter = document.getElementById("trial-outcome");
  const trialRows = [...document.querySelectorAll("#trial-table tbody tr")];
  const count = document.getElementById("trial-filter-count");
  const filterTrials = () => {
    const mode = modeFilter?.value || "";
    const outcome = outcomeFilter?.value || "";
    let visible = 0;
    for (const row of trialRows) {
      row.hidden = (mode && row.dataset.mode !== mode) || (outcome && row.dataset.outcome !== outcome);
      if (!row.hidden) visible++;
    }
    if (count) count.textContent = `${visible} of ${trialRows.length} rows`;
  };
  modeFilter?.addEventListener("change", filterTrials);
  outcomeFilter?.addEventListener("change", filterTrials);
  filterTrials();
});
