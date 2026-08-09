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
  const start = document.getElementById("start-run");
  if (start) {
    start.addEventListener("click", async () => {
      const confirmed = window.confirm(
        "Marktdaten fuer alle 110 Suchbegriffe aktualisieren? Es werden nur die aktuell freigegebenen Auktionsquellen abgefragt."
      );
      if (!confirmed) return;
      start.disabled = true;
      try {
        const result = await postJSON("/api/runs/start");
        showToast(`Marktdatenlauf #${result.run_id} wurde eingereiht.`);
        window.setTimeout(() => window.location.reload(), 900);
      } catch (error) {
        showToast(error.message, true);
        start.disabled = false;
      }
    });
  }

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
