const shell = document.querySelector('[data-question-run]');
if (shell) {
  const jobId = shell.dataset.questionRun;
  const refresh = async () => {
    try {
      const response = await fetch(`/api/questions/runs/${encodeURIComponent(jobId)}`, {cache: 'no-store'});
      if (!response.ok) return;
      const state = await response.json();
      document.getElementById('ql-job-status').textContent = state.job.status;
      document.getElementById('ql-completed').textContent = `${state.completed} / 9 attempts`;
      for (const trial of state.trials) {
        const row = shell.querySelector(`.ql-attempt[data-mode="${trial.mode}"][data-attempt="${trial.attempt}"]`);
        if (!row) continue;
        const status = row.querySelector('[data-field="status"]');
        status.textContent = trial.status;
        status.className = `ql-status ql-status-${trial.status}`;
        for (const field of ['actions', 'historical_reads', 'raw_reads']) row.querySelector(`[data-field="${field}"]`).textContent = trial[field];
        const delivery = row.querySelector('[data-field="packet_delivery"]');
        if (delivery) delivery.textContent = trial.mode === 'no-memory' ? 'No insight packet' : trial.packet_delivered ? `${trial.packet_count} insights delivered in prompt` : 'Insight packet pending';
        if (trial.id && !row.querySelector('a')) {
          const link = document.createElement('a');
          link.href = `/questions/runs/${encodeURIComponent(jobId)}/trials/${encodeURIComponent(trial.id)}`;
          link.textContent = 'Open plan and evidence trace →';
          row.querySelector('.ql-muted')?.replaceWith(link);
        }
      }
      if (state.job.status !== 'running') clearInterval(timer);
    } catch (_) { /* A transient refresh failure leaves the saved page usable. */ }
  };
  const timer = setInterval(refresh, 3000);
}
