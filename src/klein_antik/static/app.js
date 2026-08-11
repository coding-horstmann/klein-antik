function showToast(message, isError = false) {
  const toast = document.getElementById("toast");
  if (!toast) return;
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 3500);
}

async function postJSON(url, body = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest",
    },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

function bindRunControls() {
  function bindStart(buttonId, endpoint, confirmation, label) {
    const button = document.getElementById(buttonId);
    if (!button) return;
    button.addEventListener("click", async () => {
      if (!window.confirm(confirmation)) return;
      button.disabled = true;
      try {
        const result = await postJSON(endpoint);
        showToast(`${label} #${result.run_id} wurde eingereiht.`);
        window.setTimeout(() => window.location.reload(), 900);
      } catch (error) {
        showToast(error.message, true);
        button.disabled = false;
      }
    });
  }

  bindStart(
    "start-run",
    "/api/runs/start",
    "Marktdaten fuer alle 110 Suchbegriffe aktualisieren? Es werden nur die aktuell freigegebenen Auktionsquellen abgefragt.",
    "Marktdatenlauf"
  );
  bindStart(
    "start-backfill",
    "/api/runs/backfill",
    "Die jeweils naechsten zwei Archivseiten je Suchbegriff und Quelle abfragen? Bereits abgearbeitete Seiten werden nicht wiederholt.",
    "Archiv-Backfill"
  );
  bindStart(
    "start-source-pilot",
    "/api/runs/source-pilot",
    "Die vier neuen Quellen mit sieben repraesentativen Suchbegriffen testen? Preise, Links und Relevanz werden getrennt gespeichert.",
    "Quellenpilot"
  );
  bindStart(
    "start-meissen-backfill",
    "/api/runs/meissen-backfill",
    "Das Meissen-Archiv von Auctionet bis Seite 200 einlesen? Bereits verarbeitete Seiten werden uebersprungen.",
    "Meissen-Archivlauf"
  );
  bindStart(
    "start-meissen-porcelain-pilot",
    "/api/runs/meissen-porcelain-pilot",
    "Die naechsten getrennten Meissen-Quellen testen? Van Ham wird uebersprungen, weil es bereits ausgewertet wurde.",
    "Meissen-Quellenpilot"
  );
  bindStart(
    "start-meissen-porcelain-backfill",
    "/api/runs/meissen-porcelain-backfill",
    "Die naechsten zwei Quittenbaum-Archivseiten fuer Meissen einlesen? Bereits erfasste Seiten werden per Cursor uebersprungen.",
    "Meissen-Quellenarchiv"
  );

  document.querySelectorAll(".cancel-run").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm(`Lauf #${button.dataset.runId} stoppen?`)) return;
      button.disabled = true;
      try {
        await postJSON(`/api/runs/${button.dataset.runId}/cancel`);
        window.location.reload();
      } catch (error) {
        showToast(error.message, true);
        button.disabled = false;
      }
    });
  });

  const list = document.querySelector(".runs-list[data-auto-refresh=true]");
  if (list) {
    window.setTimeout(() => window.location.reload(), 12000);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  if (window.lucide) window.lucide.createIcons();
  bindRunControls();
});
