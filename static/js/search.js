const form = document.getElementById("search-form");
const input = document.getElementById("search-input");
const resultsEl = document.getElementById("results");

function currency(value) {
  return "$" + Number(value).toFixed(2);
}

function renderResults(query, results) {
  if (!results.length) {
    resultsEl.innerHTML = `<p class="results-hint">No pharmacy in the network currently has “${query}” in stock.</p>`;
    return;
  }

  const grouped = {};
  for (const r of results) {
    if (!grouped[r.medication_name]) grouped[r.medication_name] = [];
    grouped[r.medication_name].push(r);
  }

  let html = "";
  for (const [medName, rows] of Object.entries(grouped)) {
    html += `<div class="result-group">`;
    html += `<h2 class="result-med-name">${medName}</h2>`;
    html += `<ul class="result-list">`;
    rows.forEach((r, i) => {
      const bestTag = i === 0 ? `<span class="best-price-tag">Lowest price</span>` : "";
      html += `
        <li class="result-row">
          <div class="result-pharmacy">
            <span class="pharmacy-name">${r.pharmacy_name}</span>
            <span class="pharmacy-address">${r.address || ""}</span>
          </div>
          <div class="result-price">${currency(r.price)} ${bestTag}</div>
        </li>`;
    });
    html += `</ul></div>`;
  }
  resultsEl.innerHTML = html;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = input.value.trim();
  if (!query) return;

  resultsEl.innerHTML = `<p class="results-hint">Searching…</p>`;

  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
    const data = await res.json();
    renderResults(data.query, data.results || []);
  } catch (err) {
    resultsEl.innerHTML = `<p class="results-hint">Something went wrong. Please try again.</p>`;
  }
});
