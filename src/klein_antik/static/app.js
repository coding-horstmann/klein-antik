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

function reviewPayload(element) {
  return {
    content_status: element.querySelector('[name="content_status"]').value,
    use_status: element.querySelector('[name="use_status"]').value,
    tags: Array.from(element.querySelectorAll('[name="tags"]:checked')).map((input) => input.value),
    note: element.querySelector('[name="note"]').value,
  };
}

function dealReviewPayload(element) {
  return {
    review_status: element.querySelector('[name="review_status"]').value,
    tags: Array.from(element.querySelectorAll('[name="tags"]:checked')).map((input) => input.value),
    note: element.querySelector('[name="note"]').value,
  };
}

function queryPayload(element) {
  return {
    review_status: element.querySelector('[name="review_status"]').value,
    note: element.querySelector('[name="note"]').value,
  };
}

function matchReviewPayload(element) {
  return {
    review_status: element.querySelector('[name="review_status"]').value,
    note: element.querySelector('[name="note"]').value,
  };
}

function bindAutosave(selector, urlFor, payloadFor) {
  document.querySelectorAll(selector).forEach((element) => {
    let timer;
    let lastPayload = JSON.stringify(payloadFor(element));
    const state = element.querySelector(".save-state");

    const save = async () => {
      const payload = payloadFor(element);
      const serialized = JSON.stringify(payload);
      if (serialized === lastPayload) return;
      state.textContent = "Speichert …";
      state.className = "save-state saving";
      try {
        await postJSON(urlFor(element), payload);
        lastPayload = serialized;
        state.textContent = "Gespeichert";
        state.className = "save-state saved";
      } catch (error) {
        state.textContent = "Fehler";
        state.className = "save-state error";
        showToast(error.message, true);
      }
    };

    element.querySelectorAll("select, input[type=checkbox]").forEach((control) => {
      control.addEventListener("change", save);
    });
    element.querySelectorAll("textarea").forEach((control) => {
      control.addEventListener("input", () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(save, 700);
      });
      control.addEventListener("blur", save);
    });
  });
}

function bindRunControls() {
  const start = document.getElementById("start-run");
  if (start) {
    start.addEventListener("click", async () => {
      const confirmed = window.confirm(
        "Marktpreislauf für alle 110 Suchbegriffe starten? Dabei werden 238 öffentliche Quellenabfragen bei vier Auktionshäusern ausgeführt."
      );
      if (!confirmed) return;
      start.disabled = true;
      try {
        const result = await postJSON("/api/runs/start");
        showToast(`Lauf #${result.run_id} wurde eingereiht.`);
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

function bindDealRunControls() {
  const start = document.getElementById("start-deal-run");
  if (start) {
    start.addEventListener("click", async () => {
      const confirmed = window.confirm(
        "eBay DE für alle 110 Suchbegriffe starten? Der Pilot verwendet höchstens 110 offizielle Browse-API-Suchabfragen und nur Privatverkäufer."
      );
      if (!confirmed) return;
      start.disabled = true;
      try {
        const result = await postJSON("/api/deals/runs/start");
        showToast(`eBay-Lauf #${result.run_id} wurde eingereiht.`);
        window.setTimeout(() => window.location.reload(), 900);
      } catch (error) {
        showToast(error.message, true);
        start.disabled = false;
      }
    });
  }

  document.querySelectorAll(".cancel-deal-run").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm(`eBay-Lauf #${button.dataset.dealRunId} stoppen?`)) return;
      button.disabled = true;
      try {
        await postJSON(`/api/deals/runs/${button.dataset.dealRunId}/cancel`);
        window.location.reload();
      } catch (error) {
        showToast(error.message, true);
        button.disabled = false;
      }
    });
  });

  const list = document.querySelector(".deal-runs[data-deal-auto-refresh=true]");
  if (list) {
    window.setTimeout(() => window.location.reload(), 12000);
  }
}

function bindImageMatchControls() {
  const start = document.getElementById("start-image-match-run");
  if (start) {
    start.addEventListener("click", async () => {
      const confirmed = window.confirm(
        "Bildabgleich fuer alle aktuellen Deals starten? Die Bilder werden nur ueber die gespeicherten Bild-URLs geladen und gegen passende Auktionsreferenzen verglichen."
      );
      if (!confirmed) return;
      start.disabled = true;
      try {
        const result = await postJSON("/api/image-matches/runs/start");
        showToast(`Bildabgleich #${result.run_id} wurde eingereiht.`);
        window.setTimeout(() => window.location.reload(), 900);
      } catch (error) {
        showToast(error.message, true);
        start.disabled = false;
      }
    });
  }

  document.querySelectorAll(".cancel-image-match-run").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm(`Bildabgleich #${button.dataset.imageMatchRunId} stoppen?`)) return;
      button.disabled = true;
      try {
        await postJSON(`/api/image-matches/runs/${button.dataset.imageMatchRunId}/cancel`);
        window.location.reload();
      } catch (error) {
        showToast(error.message, true);
        button.disabled = false;
      }
    });
  });

  const list = document.querySelector(".image-match-runs[data-image-match-auto-refresh=true]");
  if (list) {
    window.setTimeout(() => window.location.reload(), 12000);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  if (window.lucide) window.lucide.createIcons();
  bindAutosave(
    ".review-form",
    (element) => `/api/listings/${element.dataset.listingId}/review`,
    reviewPayload
  );
  bindAutosave(
    ".deal-review-form",
    (element) => `/api/deals/${element.dataset.dealId}/review`,
    dealReviewPayload
  );
  bindAutosave(
    ".query-review",
    (element) => `/api/queries/${encodeURIComponent(element.dataset.queryId)}/review`,
    queryPayload
  );
  bindAutosave(
    ".match-review-form",
    (element) => `/api/image-matches/${element.dataset.matchId}/review`,
    matchReviewPayload
  );
  bindRunControls();
  bindDealRunControls();
  bindImageMatchControls();
});
