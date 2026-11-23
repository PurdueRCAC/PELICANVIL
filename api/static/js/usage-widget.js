function formatUsageBytes(bytes) {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const val = bytes / Math.pow(1024, i);
  return `${val < 10 ? val.toFixed(1) : Math.round(val)} ${units[i]}`;
}

function renderUsageChart(daily, container) {
  if (!daily.length) {
    container.innerHTML = '<span class="usage-chart-empty">No download data yet</span>';
    return;
  }

  const maxBytes = Math.max(...daily.map(d => d.bytes), 1);
  const barCount = daily.length;
  const chartW = 100;
  const chartH = 48;
  const barGap = 1.5;
  const barW = Math.max(2, (chartW - barGap * (barCount - 1)) / barCount);

  let bars = "";
  daily.forEach((d, i) => {
    const h = Math.max(1.5, (d.bytes / maxBytes) * chartH);
    const x = i * (barW + barGap);
    const y = chartH - h;
    const label = `${d.day}: ${formatUsageBytes(d.bytes)}`;
    bars += `<rect x="${x}" y="${y}" width="${barW}" height="${h}" rx="1" fill="var(--accent)" opacity="0.7">
      <title>${label}</title>
    </rect>`;
  });

  container.innerHTML = `
    <svg class="usage-chart-svg" viewBox="0 0 ${chartW} ${chartH}" preserveAspectRatio="none">
      ${bars}
    </svg>
    <div class="usage-chart-axis">
      <span>${daily[0].day.slice(5)}</span>
      <span>${daily[daily.length - 1].day.slice(5)}</span>
    </div>
  `;
}

function renderUsageCategories(categories, container) {
  if (!categories.length) return;
  container.innerHTML = categories.map(c => `
    <div class="usage-category-row">
      <span class="usage-category-name">${c.category}</span>
      <span class="usage-category-bytes">${formatUsageBytes(c.bytes)}</span>
    </div>
  `).join("");
}

document.addEventListener("DOMContentLoaded", async () => {
  try {
    const resp = await fetch(`${window.ROOT_PATH}/api/usage/stats?days=30`);
    if (!resp.ok) return;
    const data = await resp.json();

    const widget = document.getElementById("usage-widget");
    const statsEl = document.getElementById("usage-widget-stats");
    const chartEl = document.getElementById("usage-widget-chart");
    const catEl = document.getElementById("usage-widget-categories");
    const periodEl = document.getElementById("usage-widget-period");

    periodEl.textContent = "Last 30 days";

    statsEl.innerHTML = `
      <div class="usage-stat">
        <span class="usage-stat-value">${formatUsageBytes(data.total_bytes)}</span>
        <span class="usage-stat-label">Downloaded</span>
      </div>
      <div class="usage-stat">
        <span class="usage-stat-value">${data.total_files.toLocaleString()}</span>
        <span class="usage-stat-label">Files</span>
      </div>
      <div class="usage-stat">
        <span class="usage-stat-value">${data.total_events.toLocaleString()}</span>
        <span class="usage-stat-label">Sessions</span>
      </div>
      <div class="usage-stat usage-stat-alltime">
        <span class="usage-stat-value">${formatUsageBytes(data.all_time_bytes)}</span>
        <span class="usage-stat-label">All Time</span>
      </div>
    `;

    renderUsageChart(data.daily, chartEl);
    renderUsageCategories(data.by_category, catEl);

    widget.style.display = "";
  } catch (_) {}
});
