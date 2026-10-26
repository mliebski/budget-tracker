(function () {
  "use strict";

  const cad = new Intl.NumberFormat("en-CA", { style: "currency", currency: "CAD", maximumFractionDigits: 0 });
  const cadCents = new Intl.NumberFormat("en-CA", { style: "currency", currency: "CAD" });

  function parseAmount(text) {
    const cleaned = String(text || "").replace(/[$,\s]/g, "").replace(/-/g, "+-");
    return cleaned.split("+").reduce((sum, p) => sum + (parseFloat(p) || 0), 0);
  }

  // --- Flash messages & confirmations ------------------------------------------
  document.querySelectorAll("[data-dismiss]").forEach((btn) =>
    btn.addEventListener("click", () => btn.closest(".flash").remove()));
  if (window.history.replaceState && /[?&](msg|undo|error)=/.test(location.search)) {
    // Drop the message from the URL so a reload doesn't show it again.
    const url = new URL(location.href);
    ["msg", "undo", "error"].forEach((k) => url.searchParams.delete(k));
    history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
  document.querySelectorAll("form[data-confirm]").forEach((form) =>
    form.addEventListener("submit", (e) => { if (!confirm(form.dataset.confirm)) e.preventDefault(); }));
  document.querySelectorAll("select[data-autosubmit]").forEach((sel) =>
    sel.addEventListener("change", () => sel.form.submit()));

  // --- Expandable table groups ---------------------------------------------------
  document.querySelectorAll("[data-expand]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const open = btn.getAttribute("aria-expanded") !== "true";
      btn.setAttribute("aria-expanded", String(open));
      btn.closest("tbody").querySelectorAll("tr.sub").forEach((tr) => { tr.hidden = !open; });
    }));

  // --- Entry forms: expense/income toggle filters categories ------------------------
  document.querySelectorAll("form[data-entry-form]").forEach((form) => {
    const select = form.querySelector("select[data-category-select]");
    if (!select) return;
    const original = select.cloneNode(true);
    const chips = form.querySelectorAll(".chip[data-cat]");
    const radios = form.querySelectorAll('input[name="kind"]');

    function currentKind() {
      const r = form.querySelector('input[name="kind"]:checked');
      return r ? r.value : "expense";
    }
    function markChips() {
      chips.forEach((c) => c.classList.toggle("on", c.dataset.cat === select.value));
    }
    function applyKind() {
      const kind = currentKind();
      const value = select.value;
      select.innerHTML = "";
      Array.from(original.children).forEach((node) => {
        if (node.tagName === "OPTGROUP" && node.dataset.kind !== kind) return;
        select.appendChild(node.cloneNode(true));
      });
      const stillThere = Array.from(select.options).some((o) => o.value === value);
      select.value = stillThere ? value : "";
      chips.forEach((c) => { c.hidden = c.dataset.kind !== kind; });
      markChips();
    }
    radios.forEach((r) => r.addEventListener("change", applyKind));
    chips.forEach((chip) => chip.addEventListener("click", () => {
      select.value = chip.dataset.cat;
      markChips();
      const amount = form.querySelector('input[name="amount"]');
      if (amount && !amount.value) amount.focus();
    }));
    select.addEventListener("change", markChips);
    // Pick the matching toggle when an existing category of the other kind is selected.
    const selected = select.selectedOptions[0];
    if (selected && selected.dataset.kind) {
      const radio = form.querySelector(`input[name="kind"][value="${selected.dataset.kind}"]`);
      if (radio) radio.checked = true;
    }
    applyKind();
  });

  // --- Receipts: scan on pick, busy indicators, warranty length shortcuts ---------------
  document.querySelectorAll("form[data-scan]").forEach((form) => {
    form.querySelectorAll('input[type="file"]').forEach((input) =>
      input.addEventListener("change", () => {
        if (!input.files.length) return;
        // Only the chosen input is sent; reading can take a few seconds.
        form.querySelectorAll('input[type="file"]').forEach((other) => { if (other !== input) other.disabled = true; });
        form.querySelector(".busy").hidden = false;
        form.querySelectorAll("label.btn").forEach((b) => b.setAttribute("aria-disabled", "true"));
        form.submit();
      }));
  });
  // Phone browsers restore a page from memory on Back, including the "busy" state set above.
  window.addEventListener("pageshow", (e) => {
    if (!e.persisted) return;
    document.querySelectorAll("form[data-scan], form[data-upload]").forEach((form) => {
      form.reset();
      form.querySelectorAll('input[type="file"]').forEach((i) => { i.disabled = false; });
      form.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      form.querySelectorAll("label.btn").forEach((b) => b.removeAttribute("aria-disabled"));
      const busy = form.querySelector(".busy");
      if (busy) busy.hidden = true;
    });
  });
  document.querySelectorAll("form[data-upload]").forEach((form) =>
    form.addEventListener("submit", () => {
      form.querySelector(".busy").hidden = false;
      form.querySelector("button").disabled = true;
    }));
  document.querySelectorAll("[data-warranty-years]").forEach((group) => {
    const form = group.closest("form");
    const end = form.querySelector("[data-warranty-end]");
    group.querySelectorAll("[data-years]").forEach((btn) =>
      btn.addEventListener("click", () => {
        const from = form.querySelector('input[name="purchase_date"], input[name="date"]');
        const d = from && from.value ? new Date(from.value + "T12:00:00") : new Date();
        d.setFullYear(d.getFullYear() + Number(btn.dataset.years));
        end.value = d.toISOString().slice(0, 10);
        group.querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c === btn));
      }));
  });

  // A purchase with a warranty is a big one: pre-tick "one-off" so it stays out of the forecast average.
  document.querySelectorAll("form[data-warranty-oneoff]").forEach((form) => {
    const oneOff = form.querySelector('input[name="one_off"]');
    const fields = form.querySelectorAll('[data-warranty-end], input[name="serial_number"]');
    const sync = () => {
      if (!oneOff || oneOff.dataset.touched) return;
      oneOff.checked = Array.from(fields).some((f) => f.value.trim());
      if (oneOff.checked) oneOff.closest("details").open = true;
    };
    oneOff.addEventListener("change", () => { oneOff.dataset.touched = "1"; });
    fields.forEach((f) => f.addEventListener("input", sync));
    form.querySelectorAll("[data-years]").forEach((b) => b.addEventListener("click", sync));
  });

  // --- AI: wait for a report, chat with streamed answers --------------------------------
  const poll = document.querySelector("[data-ai-poll]");
  if (poll) {
    const check = () => fetch(`/ai/reports/${poll.dataset.aiPoll}/status`, { cache: "no-store" })
      .then((r) => r.json())
      .then((s) => { if (s.status !== "running") location.reload(); else setTimeout(check, 4000); })
      .catch(() => setTimeout(check, 8000));
    setTimeout(check, 4000);
  }

  // A small Markdown subset for model answers. Everything is escaped first, so the
  // model's text can never inject markup.
  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function inline(s) {
    return s.replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  }
  function markdown(src) {
    const lines = escapeHtml(src.replace(/\r/g, "")).split("\n");
    const out = [];
    let list = null, para = [], table = [];
    const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join("<br>"))}</p>`); para = []; } };
    const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null; } };
    const flushTable = () => {
      if (!table.length) return;
      const rows = table.filter((r) => !/^\|?\s*:?-{2,}/.test(r)).map((r) => r.replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim())));
      const [head, ...body] = rows;
      out.push(`<div class="md-table"><table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>` +
        body.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("") + "</tbody></table></div>");
      table = [];
    };
    for (const line of lines) {
      const t = line.trim();
      let m;
      if (t.startsWith("|")) { flushPara(); flushList(); table.push(t); continue; }
      flushTable();
      if (!t) { flushPara(); flushList(); continue; }
      if ((m = t.match(/^#{1,6}\s+(.*)/))) { flushPara(); flushList(); out.push(`<h4>${inline(m[1])}</h4>`); continue; }
      if ((m = t.match(/^[-*•]\s+(.*)/)) || (m = t.match(/^\d+[.)]\s+(.*)/))) {
        flushPara();
        const tag = /^\d/.test(t) ? "ol" : "ul";
        if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
        list.items.push(m[1]);
        continue;
      }
      flushList();
      para.push(t);
    }
    flushPara(); flushList(); flushTable();
    return out.join("");
  }
  document.querySelectorAll("[data-markdown]").forEach((el) => { el.innerHTML = markdown(el.textContent); });

  const chatBox = document.querySelector("[data-ai-chat]");
  if (chatBox) {
    const form = chatBox.querySelector("[data-ai-form]");
    const input = form.querySelector("textarea");
    const list = chatBox.querySelector("[data-messages]");
    const button = form.querySelector("button");
    const scroll = () => { list.scrollTop = list.scrollHeight; };
    const bubble = (role, html) => {
      const empty = list.querySelector("[data-empty]");
      if (empty) empty.remove();
      const row = document.createElement("div");
      row.className = `msg ${role}`;
      row.innerHTML = `<div class="bubble">${html}</div>`;
      list.appendChild(row);
      scroll();
      return row.firstChild;
    };
    scroll();
    chatBox.querySelectorAll("[data-ask]").forEach((chip) => chip.addEventListener("click", () => {
      input.value = chip.textContent.trim();
      form.requestSubmit();
    }));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const question = input.value.trim();
      if (!question || button.disabled) return;
      bubble("user", escapeHtml(question));
      input.value = "";
      button.disabled = true;
      const answer = bubble("assistant", '<span class="thinking">Reading your budget…</span>');
      let text = "";
      try {
        const body = new FormData();
        body.append("message", question);
        body.append("chat_id", chatBox.dataset.chatId || "");
        const response = await fetch("/ai/chat/send", { method: "POST", body });
        if (!response.ok) throw new Error((await response.json()).error || "The question could not be sent.");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split("\n");
          buffer = parts.pop();
          for (const part of parts) {
            if (!part.trim()) continue;
            const ev = JSON.parse(part);
            if (ev.t === "chat" && !chatBox.dataset.chatId) {
              chatBox.dataset.chatId = ev.id;
              history.replaceState(null, "", `/ai/chat/${ev.id}`);
              // Show the new chat in the side list without a reload.
              const side = document.querySelector(".ai-chats");
              if (side) {
                let ul = side.querySelector("ul.list");
                if (!ul) {
                  const empty = side.querySelector(".empty");
                  if (empty) empty.remove();
                  ul = document.createElement("ul");
                  ul.className = "list";
                  side.appendChild(ul);
                }
                const li = document.createElement("li");
                li.className = "current";
                li.innerHTML = `<a class="grow" href="/ai/chat/${ev.id}">${escapeHtml(question.slice(0, 120))}</a>`;
                ul.prepend(li);
              }
            } else if (ev.t === "thinking" && !text) {
              answer.innerHTML = '<span class="thinking">Thinking…</span>';
            } else if (ev.t === "text") {
              text += ev.c;
              answer.innerHTML = markdown(text);
              scroll();
            } else if (ev.t === "error") {
              answer.innerHTML = markdown(text) + `<p class="error">${escapeHtml(ev.c)}</p>`;
            }
          }
        }
        if (!text && !answer.querySelector(".error")) answer.innerHTML = '<p class="error">No answer came back.</p>';
      } catch (err) {
        answer.innerHTML = `<p class="error">${escapeHtml(err.message)}</p>`;
      } finally {
        button.disabled = false;
        input.focus();
      }
    });
  }

  // --- Budget form: live yearly totals ------------------------------------------------
  const budgetForm = document.querySelector("form[data-budget-form]");
  if (budgetForm) {
    const recalc = () => {
      const kinds = {};
      budgetForm.querySelectorAll("tbody[data-group]").forEach((group) => {
        let groupTotal = 0;
        group.querySelectorAll("tr[data-line]").forEach((tr) => {
          const total = parseAmount(tr.querySelector("[data-monthly]").value) * 12 +
                        parseAmount(tr.querySelector("[data-annual]").value);
          tr.querySelector("[data-line-total]").textContent = cad.format(total);
          groupTotal += total;
        });
        const gt = group.querySelector("[data-group-total]");
        if (gt) gt.textContent = cad.format(groupTotal);
        kinds[group.dataset.kind] = (kinds[group.dataset.kind] || 0) + groupTotal;
      });
      Object.entries(kinds).forEach(([k, v]) => {
        const el = budgetForm.querySelector(`[data-kind-total="${k}"]`);
        if (el) el.textContent = cad.format(v) + " / year";
      });
    };
    budgetForm.addEventListener("input", recalc);
  }

  // --- Charts ------------------------------------------------------------------------
  if (!window.Chart) return;
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const ROLE = {
    actual: v("--bar-fill"),
    budget: dark ? "#8a9a96" : "#7b8a86",
    forecast: v("--bar-proj"),
    income: v("--pos"),
    expense: v("--neg"),
    net: dark ? "#7aa7ff" : "#2f5fd0",
    recurring: dark ? "#7aa7ff" : "#2f5fd0",
    variable: dark ? "#2cb1a3" : "#0f766e",
    planned: dark ? "#e3b341" : "#b7791f",
  };
  const PALETTE = dark
    ? ["#2cb1a3", "#7aa7ff", "#e3b341", "#f08a5d", "#b794f4", "#4cc79a", "#f687b3", "#90cdf4", "#c6a67a", "#a0aec0"]
    : ["#0f766e", "#2f5fd0", "#b7791f", "#c2410c", "#7c3aed", "#11795a", "#be185d", "#0284c7", "#8b5e34", "#64748b"];

  Chart.defaults.color = v("--muted");
  Chart.defaults.borderColor = v("--border");
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.animation = false;

  document.querySelectorAll("canvas[data-chart]").forEach((canvas) => {
    const spec = JSON.parse(document.getElementById(canvas.dataset.chart).textContent);
    const isDoughnut = spec.type === "doughnut";
    const datasets = spec.datasets.map((ds, i) => {
      const color = ROLE[ds.role] || PALETTE[0];
      // Only datasets that share an explicit stack are stacked; lines never stack.
      // (A "stack" on a doughnut dataset makes Chart.js 4 throw and draw nothing.)
      const out = { label: ds.label, data: ds.data };
      if (!isDoughnut) out.stack = ds.stack || `own-${i}`;
      if (isDoughnut) {
        out.backgroundColor = spec.labels.map((_, i) => PALETTE[i % PALETTE.length]);
        out.borderColor = v("--surface");
        out.borderWidth = 2;
      } else if ((ds.type || spec.type) === "line") {
        Object.assign(out, { type: "line", borderColor: color, backgroundColor: color, borderWidth: 2,
                             pointRadius: 2, tension: 0.25, order: 0, spanGaps: true });
        if (ds.role === "budget") out.borderDash = [6, 4];
      } else {
        Object.assign(out, { backgroundColor: color, borderRadius: 4, order: 1, maxBarThickness: 28 });
      }
      return out;
    });
    const money = (val) => (Math.abs(val) >= 1000 ? cad.format(val) : cadCents.format(val));
    const options = {
      maintainAspectRatio: false,
      responsive: true,
      // Horizontal bars list categories down the page, so pick the hovered row by its y position;
      // with the default x axis the tooltip showed other rows' text.
      interaction: { mode: isDoughnut ? "nearest" : "index", intersect: false, axis: spec.horizontal ? "y" : "x" },
      plugins: {
        legend: { position: isDoughnut ? "right" : "bottom", labels: { boxWidth: 12, usePointStyle: true } },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const val = isDoughnut ? ctx.parsed : (spec.horizontal ? ctx.parsed.x : ctx.parsed.y);
              return val == null ? null : ` ${ctx.dataset.label || ctx.label}: ${money(val)}`;
            },
          },
        },
      },
    };
    if (isDoughnut) {
      options.plugins.tooltip.callbacks.label = (ctx) => ` ${ctx.label}: ${money(ctx.parsed)}`;
      if (window.innerWidth < 600) options.plugins.legend.position = "bottom";
    } else {
      const valueAxis = { beginAtZero: true, stacked: !!spec.stacked, ticks: { callback: (x) => cad.format(x) }, grid: { color: v("--border") } };
      const catAxis = { stacked: !!spec.stacked, grid: { display: false } };
      options.indexAxis = spec.horizontal ? "y" : "x";
      options.scales = spec.horizontal ? { x: valueAxis, y: catAxis } : { x: catAxis, y: valueAxis };
    }
    new Chart(canvas, { type: isDoughnut ? "doughnut" : "bar", data: { labels: spec.labels, datasets }, options });
  });
})();
