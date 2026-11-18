/* Shared ACT! profile status and directory controls for Advisor Map and Field. */
(function (root) {
  "use strict";
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
  const place = (a) => [a.city, a.state].filter(Boolean).join(", ");
  const address = (a) => [a.line1, a.line2, a.line3, place(a), a.postalCode].filter(Boolean).join(", ");
  const chip = (label, tone = "") => `<span class="act-chip ${tone}">${esc(label)}</span>`;
  let locate;

  function slot(crd) {
    return /^\d{3,12}$/.test(String(crd))
      ? `<section class="act-profile" data-act-profile="${esc(crd)}" aria-label="ACT! contact">
          ${chip("Checking ACT!")}<p role="status"></p></section>` : "";
  }

  function render(state) {
    const labels = {
      linked: "? In ACT!", added: "? Added to ACT!", review: "ACT! match needs review",
      unlinked: "No ACT! link", pending: "ACT! addition needs verification", unknown: "ACT! status unavailable",
    };
    const linked = state.status === "linked" || state.status === "added";
    const primary = state.primary && address(state.primary);
    return chip(labels[state.status] || labels.unknown, linked ? "yes" : "")
      + (primary ? `<p><b>ACT! primary address</b> <span>${esc(primary)}</span></p>` : "")
      + (state.notice ? `<p>${esc(state.notice)}</p>` : "")
      + (state.candidates || []).map((candidate) => {
        const city = place(candidate.address);
        const source = /^https?:\/\//i.test(candidate.url || "")
          ? `<a href="${esc(candidate.url)}" rel="noopener noreferrer" target="_blank">Source profile</a>` : "";
        return `<div class="act-office-note"><p><b>${esc(candidate.source)} lists this advisor in ${esc(city)}.</b></p>
          <p>${esc(address(candidate.address))} ${source}</p>
          <button type="button" class="ask-btn" data-act-action="replace_primary" data-candidate="${esc(candidate.id)}">
            Replace ACT! primary address with ${esc(city)}</button></div>`;
      }).join("")
      + (state.draft ? '<button type="button" class="ask-btn" data-act-action="add_contact">Add to ACT!</button>' : "")
      + '<p class="act-result" role="status"></p>';
  }

  async function request(query, body) {
    const response = await root.fetch("/api/contact-address" + query, {
      credentials: "same-origin", cache: "no-store",
      ...(body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
    });
    if (!(response.headers.get("content-type") || "").includes("application/json"))
      throw new Error("Sign in again to check ACT!.");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "ACT! is temporarily unavailable.");
    return data;
  }

  function openDialog(title) {
    const dialog = root.document.createElement("dialog");
    dialog.className = "act-dialog";
    dialog.setAttribute("aria-label", title);
    dialog.innerHTML = `<header><h2>${esc(title)}</h2><form method="dialog">
      <button type="submit" class="ask-btn">Close</button></form></header><div class="act-dialog-body"></div>`;
    root.document.body.append(dialog);
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    dialog.showModal();
    return dialog;
  }

  // First review the server-provided fields in context. Continue then asks for
  // a separate final confirmation before issuing the first write request.
  function confirmChange(action, state, candidate) {
    const adding = action === "add_contact";
    const verb = adding ? "Add contact to ACT!" : "Replace ACT! primary address";
    const dialog = openDialog(verb);
    const body = dialog.querySelector(".act-dialog-body");
    body.innerHTML = (adding
      ? `<p><b>${esc(state.draft.fullName)}</b> ? CRD ${esc(state.draft.customFields.crd)}</p>
          <p>${esc(state.draft.company)} ? ${esc(state.draft.emailAddress)}</p>`
      : `<p><b>Current ACT! primary</b> ${esc(address(state.primary) || "No address")}</p>
         <p><b>New ACT! primary</b> ${esc(address(candidate.address))}</p>`)
      + '<button type="button" class="ask-btn primary" data-act-confirm>Continue</button>';
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(false), { once: true });
      body.querySelector("[data-act-confirm]").onclick = () => {
        const accepted = root.confirm(`${verb} now?`);
        if (accepted) {
          resolve(true);
          dialog.close();
        }
      };
    });
  }

  async function mount(element) {
    if (element.dataset.actMounted) return;
    element.dataset.actMounted = "1";
    const crd = element.dataset.actProfile;
    let state;
    try { state = await request("?crd=" + encodeURIComponent(crd)); }
    catch (error) { element.innerHTML = render({ status: "unknown", notice: error.message }); return; }
    element.innerHTML = render(state);
    let busy = false;
    element.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-act-action]");
      if (!button || busy) return;
      const action = button.dataset.actAction;
      const candidate = state.candidates?.find((row) => row.id === button.dataset.candidate);
      if (action === "replace_primary" && !candidate) return;
      busy = true;
      try {
        if (!await confirmChange(action, state, candidate)) return;
        const status = element.querySelector(".act-result");
        status.textContent = "Saving to ACT!?";
        try {
          const saved = await request("", { crd, action, candidateId: candidate?.id,
            revision: state.revision, confirmed: true });
          if (!saved.verified) throw new Error("ACT! could not verify this change.");
          state = { status: "linked", ...saved };
          element.innerHTML = render(state);
          element.querySelector(".act-result").textContent = action === "add_contact"
            ? "Contact created and verified in ACT!. The CRD link will be checked at the next refresh."
            : "Primary address saved in ACT!. The map pin updates at the next refresh.";
        } catch (error) {
          status.textContent = error.message + " Reopen this profile to check the current ACT! record.";
          element.querySelectorAll("[data-act-action]").forEach((item) => { item.disabled = true; });
        }
      } finally { busy = false; }
    });
  }

  function directory() {
    const dialog = openDialog("ACT! contacts without a matching CRD");
    const body = dialog.querySelector(".act-dialog-body");
    body.innerHTML = `<form data-act-search><label>Search by name, email, firm or location
      <input type="search" name="search" required minlength="2" maxlength="120"></label>
      <button type="submit" class="ask-btn">Search</button></form>
      <p role="status"></p><ul class="act-directory-results"></ul>`;
    const form = body.querySelector("form");
    const status = body.querySelector("[role=status]");
    const results = body.querySelector("ul");
    let sequence = 0;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const current = ++sequence;
      results.replaceChildren();
      status.textContent = "Searching ACT! contacts?";
      try {
        const result = await request("?search=" + encodeURIComponent(form.elements.search.value.trim()));
        if (current !== sequence || !dialog.open) return;
        status.textContent = result.count > result.contacts.length
          ? `Showing ${result.contacts.length} of ${result.count}. Narrow the search to see more.`
          : `${result.count} matching contact${result.count === 1 ? "" : "s"}`;
        results.innerHTML = result.contacts.map((contact, index) => `<li>
          <h3>${esc(contact.name)}</h3>${chip("? In ACT!", "yes")} ${chip("No matching CRD")}
          <p>${esc(contact.company)}<br>${esc(contact.email)}</p>
          <p>${esc(address(contact.primary) || "No ACT! primary address on file")}</p>
          ${locate && contact.coordinates
            ? `<button type="button" class="ask-btn" data-act-locate="${index}">Show on map</button>` : ""}
        </li>`).join("");
        results.onclick = (click) => {
          const button = click.target.closest("[data-act-locate]");
          if (!button || !locate) return;
          const contact = result.contacts[Number(button.dataset.actLocate)];
          if (contact?.coordinates) { locate(contact); dialog.close(); }
        };
      } catch (error) {
        if (current === sequence && dialog.open) status.textContent = error.message;
      }
    });
  }

  function scan(node) {
    if (node?.nodeType !== 1) return;
    if (node.matches("[data-act-profile]")) mount(node);
    node.querySelectorAll("[data-act-profile]").forEach(mount);
  }
  root.ActAddresses = { slot, render, setLocator: (callback) => { locate = callback; } };
  if (root.document && root.MutationObserver) {
    const start = () => {
      scan(root.document.body);
      new root.MutationObserver((changes) => changes.forEach((change) => change.addedNodes.forEach(scan)))
        .observe(root.document.body, { childList: true, subtree: true });
      root.document.addEventListener("click", (event) => {
        if (event.target.closest("[data-act-directory]")) directory();
      });
    };
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", start);
    else start();
  }
})(typeof window !== "undefined" ? window : globalThis);
