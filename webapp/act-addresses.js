/* Shared ACT! profile status and directory controls for Advisor Map and Field. */
(function (root) {
  "use strict";
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
  const place = (a) => [a.city, a.state].filter(Boolean).join(", ");
  const address = (a) => [a.line1, a.line2, a.line3, place(a), a.postalCode].filter(Boolean).join(", ");
  const chip = (label, tone = "") => `<span class="act-chip ${tone}">${esc(label)}</span>`;
  const states = {
    linked: "In ACT!", added: "Added to ACT!", review: "ACT! match needs review",
    unlinked: "Not in ACT!", pending: "ACT! addition needs verification", unknown: "ACT! status unavailable",
  };
  let locate;
  let openFirm;
  let assetBook;

  function refreshAssetDisplay() {
    for (const field of root.document.querySelectorAll("[data-act-assets]")) {
      const selected = root.AssetProductFilters?.accountValues(assetBook?.(), [field.dataset.actAssets]);
      const total = [...(selected?.values() || [])].reduce((sum, vector) =>
        sum + (vector[4] ?? vector.slice(0, 4).reduce((subtotal, value) => subtotal + value, 0)), 0);
      field.hidden = !selected?.size;
      field.querySelector("span").textContent = total.toLocaleString("en-US", {
        style:"currency", currency:"USD", maximumFractionDigits:2,
      });
    }
  }

  function badge(state, crd = "") {
    const label = states[state] || "Checking ACT!";
    const tone = state === "linked" || state === "added" ? " linked" : state === "unlinked" ? " unlinked" : "";
    return `<span class="act-badge${tone}" ${crd ? `data-act-badge="${esc(crd)}"` : ""}
      title="${label}" role="img" aria-label="${label}"><img src="act-icon.png" alt=""></span>`;
  }

  function badgeSlot(crd) {
    return /^\d{3,12}$/.test(String(crd)) ? badge("", crd) : "";
  }

  function slot(crd) {
    return /^\d{3,12}$/.test(String(crd))
      ? `<section class="act-profile" data-act-profile="${esc(crd)}" aria-label="ACT! contact">
          <p class="act-result" role="status"></p></section>` : "";
  }

  function officeSlot(crd) {
    return /^\d{3,12}$/.test(String(crd))
      ? `<div class="act-office-alerts" data-act-office="${esc(crd)}" hidden></div>` : "";
  }

  const resultSlot = '<p class="act-result" role="status"></p>';

  function render(state) {
    const statusNote = ["review", "pending", "unknown"].includes(state.status) ? states[state.status] : "";
    return (state.draft ? '<button type="button" class="ask-btn" data-act-action="add_contact">Add contact to ACT!</button>' : "")
      + (statusNote ? `<p class="act-status-note">${esc(statusNote)}</p>` : "")
      + (state.notice && state.status !== "linked" ? `<p>${esc(state.notice)}</p>` : "")
      + resultSlot;
  }

  function renderOffices(state) {
    return ((state.status === "linked" && state.notice) ? `<p class="act-office-info">${esc(state.notice)}</p>` : "")
      + (state.candidates || []).map((candidate) => {
        const city = place(candidate.address);
        const source = /^https?:\/\//i.test(candidate.url || "")
          ? `<a class="act-source-link" href="${esc(candidate.url)}" rel="noopener noreferrer" target="_blank">Source profile <span aria-hidden="true">&#8599;</span></a>` : "";
        return `<div class="act-office-note"><div class="act-office-heading"><b>${esc(candidate.source)} lists ${esc(city)}</b>${source}</div>
          <p>${esc(address(candidate.address))}</p>
          <button type="button" class="ask-btn" data-act-action="replace_primary" data-candidate="${esc(candidate.id)}">
            Replace ACT! primary address with ${esc(city)}</button></div>`;
      }).join("")
      + resultSlot;
  }

  async function request(query, body, signal) {
    const response = await root.fetch("/api/contact-address" + query, {
      credentials: "same-origin", cache: "no-store", signal,
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

  // The Add button opens a review of the server-provided fields. Submitting
  // that review is the confirmation for creation. Address replacement keeps
  // its separate final confirmation.
  function confirmChange(action, state, candidate) {
    const adding = action === "add_contact";
    const verb = adding ? "Add contact to ACT!" : "Replace ACT! primary address";
    const dialog = openDialog(verb);
    const body = dialog.querySelector(".act-dialog-body");
    if (adding) {
      const draft = state.draft;
      const fields = [["Company", draft.company], ["Email", draft.emailAddress], ["Team", draft.department],
        ["Title", draft.jobTitle], ["Salutation", draft.salutation],
        ["Office phone", [draft.businessPhone, draft.businessExtension && "ext. " + draft.businessExtension].filter(Boolean).join(" ")],
        ["Mobile phone", draft.mobilePhone], ["Website", draft.website],
        ["Business address", draft.businessAddress && address(draft.businessAddress)],
        ["Mail Code", draft.customFields.email__y_n === "2" ? "Email only" : draft.customFields.email__y_n]];
      body.innerHTML = `<p><b>${esc(draft.fullName)}</b> / CRD ${esc(draft.customFields.crd)}</p>
        <dl class="act-create-fields">${fields.filter(([, value]) => value).map(([label, value]) =>
          `<dt>${label}</dt><dd>${esc(value)}</dd>`).join("")}</dl>
        <p>EIC Contact can be set on the new record in ACT!.</p>
        <button type="button" class="ask-btn primary" data-act-confirm>Add contact</button>`;
    } else {
      body.innerHTML = `<p><b>Current ACT! primary</b> ${esc(address(state.primary) || "No address")}</p>
        <p><b>New ACT! primary</b> ${esc(address(candidate.address))}</p>
        <button type="button" class="ask-btn primary" data-act-confirm>Continue</button>`;
    }
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(false), { once: true });
      body.querySelector("[data-act-confirm]").onclick = () => {
        const accepted = adding || root.confirm(`${verb} now?`);
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
    const office = element.parentElement.querySelector(`[data-act-office="${crd}"]`);
    const update = (value) => {
      if (!element.isConnected) return;
      element.innerHTML = render(value);
      if (office) {
        office.innerHTML = renderOffices(value);
        office.hidden = !value.candidates?.length && !(value.status === "linked" && value.notice);
      }
      const header = root.document.querySelector(`[data-act-badge="${crd}"]`);
      if (header) header.outerHTML = badge(value.status, crd);
    };
    let state;
    try { state = await request("?crd=" + encodeURIComponent(crd)); }
    catch (error) { update({ status: "unknown", notice: error.message }); return; }
    update(state);
    let busy = false;
    const handleAction = async (event) => {
      const host = event.currentTarget;
      const button = event.target.closest("[data-act-action]");
      if (!button || busy) return;
      const action = button.dataset.actAction;
      const candidate = state.candidates?.find((row) => row.id === button.dataset.candidate);
      if (action === "replace_primary" && !candidate) return;
      busy = true;
      try {
        if (!await confirmChange(action, state, candidate)) return;
        const status = host.querySelector(".act-result");
        status.textContent = "Saving to ACT!\u2026";
        try {
          const saved = await request("", { crd, action, candidateId: candidate?.id,
            revision: state.revision, confirmed: true });
          if (!saved.verified) throw new Error("ACT! could not verify this change.");
          state = { status: "linked", ...saved };
          update(state);
          host.hidden = false;
          host.querySelector(".act-result").textContent = action === "add_contact"
            ? (saved.unverifiedFields?.length
              ? "Contact created. Review these fields in ACT!: " + saved.unverifiedFields.join(", ") + "."
              : "Contact created and verified in ACT!. The CRD link will be checked at the next refresh.")
            : "Primary address saved in ACT!. The map pin updates at the next refresh.";
        } catch (error) {
          status.textContent = error.message + " Reopen this profile to check the current ACT! record.";
          host.querySelectorAll("[data-act-action]").forEach((item) => { item.disabled = true; });
        }
      } finally { busy = false; }
    };
    element.addEventListener("click", handleAction);
    office?.addEventListener("click", handleAction);
  }

  const directory = new Map();
  const isContactKey = key => /^act-[a-f0-9]{26}$/.test(String(key || ""));
  const current = key => directory.get(String(key));
  function snapshot(contact) {
    const dial = root.Dial;
    return { crd: contact.contactKey, name: contact.name, firm: contact.firm || contact.company,
      phone: contact.phone || "", phoneKind: "office", city: contact.primary?.city || "",
      state: contact.primary?.state || "", email: contact.email || "",
      identityApproved: true, contactTier: "confirmed", contactSource: "act-only",
      emailConfirmed: contact.emailApproved === true, emailEligibilityKnown: true,
      emailTierKey: dial.emailTierKey(), contactRouteVersion: dial.state.contactRouteVersion,
      firstName: contact.greeting || "", lastName: contact.lastName || "", identityLabel: "ACT! contact",
    };
  }

  async function loadListContacts(items) {
    const keys = [...new Set(items.map(item => String(item.crd)).filter(isContactKey))];
    const out = [];
    for (let start = 0; start < keys.length; start += 50) {
      const batch = keys.slice(start, start + 50);
      const result = await request("?keys=" + encodeURIComponent(batch.join(",")));
      batch.forEach(key => directory.delete(key));
      for (const contact of result.contacts) {
        directory.set(contact.contactKey, contact);
        out.push(contact);
      }
    }
    return out;
  }

  async function openByKey(key) {
    const [contact] = await loadListContacts([{ crd: key }]);
    if (!contact) throw new Error("This ACT! contact is no longer in the shared directory.");
    contactDetails(contact);
  }

  function contactDetails(contact) {
    const dialog = openDialog(contact.name);
    const body = dialog.querySelector(".act-dialog-body");
    const dial = root.Dial;
    const key = contact.contactKey;
    const tel = key && dial?.state.ready && dial.telHref(key, contact.phone);
    const firm = contact.firm || contact.company;
    const firmLink = firm && /^\d+$/.test(contact.firmCrd || "") && openFirm
      ? `<button type="button" class="ask-btn" data-act-firm>${esc(firm)}</button>` : esc(firm);
    if (isContactKey(key)) directory.set(key, contact);
    body.innerHTML = `<p>${badge("linked")} ${chip("No matching CRD")}</p>
      ${contact.title ? `<p>${esc(contact.title)}</p>` : ""}
      ${firm ? `<p><b>Firm</b> ${firmLink}${contact.firm ? ' <small>Matched by email domain</small>' : ""}</p>` : ""}
      <p><b>Working office from ACT!</b> ${esc(address(contact.primary) || "Address unavailable")}</p>
      <p data-act-assets="${esc(key)}" hidden><b>EIC Assets</b> <span></span></p>
      ${contact.phone ? `<p><b>Phone</b> ${esc(contact.phone)}${!tel && dial?.isDnc(key) ? " ? Do not call" : ""}</p>` : ""}
      ${contact.email ? `<p><b>Email</b> ${esc(contact.email)}${key && !contact.emailApproved ? " ? Needs review" : ""}</p>` : ""}
      <div class="act-contact-actions">
        ${tel ? `<a class="ask-btn" href="${esc(tel)}" data-act-call>Call</a>` : ""}
        ${key && contact.emailApproved ? '<button type="button" class="ask-btn" data-act-email>Email</button>' : ""}
        ${locate && contact.coordinates ? '<button type="button" class="ask-btn" data-act-pin>Show on map</button>' : ""}
      </div>
      ${key && dial ? '<div class="act-list-control"></div>' : ""}
      <p class="act-result" role="status"></p>`;
    refreshAssetDisplay();
    const status = body.querySelector(".act-result");
    body.querySelector("[data-act-pin]")?.addEventListener("click", () => { locate(contact); dialog.close(); });
    body.querySelector("[data-act-firm]")?.addEventListener("click", () => { dialog.close(); openFirm(contact.firmCrd); });
    body.querySelector("[data-act-call]")?.addEventListener("click", event => {
      if (dial.isDnc(key)) {
        event.preventDefault();
        status.textContent = "This contact is on the do-not-call list.";
        return;
      }
      dial.log({ crd:key, kind:"call", name:contact.name, firm:firm || "", phone:contact.phone,
        phoneKind:"office" }).catch(() => {});
    });
    body.querySelector("[data-act-email]")?.addEventListener("click", () => {
      dialog.close();
      root.EmailComposer?.open([snapshot(contact)]);
    });
    const picker = body.querySelector(".act-list-control");
    if (picker) {
      const owner = root.AdvisorListWorkspace?.();
      (owner ? dial.readLists(owner.id) : dial.loadLists()).then(lists => {
        if (!dialog.isConnected) return;
        const choices = lists.filter(list => !/^(?:role-|__)/.test(list.id));
        picker.innerHTML = choices.length
          ? `<label class="email-label">${owner ? `Add to ${esc(owner.email)}'s list` : "Add to a list"} <select>${choices.map(list =>
              `<option value="${esc(list.id)}">${esc(list.name)}</option>`).join("")}</select></label>
             <button type="button" class="ask-btn">Add</button>`
          : '<p>Create a list in Lists first.</p>';
        const add = picker.querySelector("button");
        if (!add) return;
        add.onclick = async () => {
          add.disabled = true;
          try {
            const saved = await dial.addToList(picker.querySelector("select").value, snapshot(contact), owner?.id);
            status.textContent = saved.added === false ? "Already on this list." : "Added to the list.";
          } catch (error) { status.textContent = error.message; }
          finally { add.disabled = false; }
        };
      }).catch(error => { status.textContent = error.message; });
    }
  }

  const searches = new WeakMap();
  function searchInto(host, query, onChange = () => {}, onSelect = () => {}) {
    query = String(query || "").trim().toLowerCase();
    const previous = searches.get(host);
    if (query && previous?.query === query) return;
    previous?.controller.abort();
    searches.delete(host);
    host.replaceChildren();
    host.hidden = true;
    onChange();
    if (query.length < 2 || !/[^\d\s]/.test(query)) return;
    const controller = new AbortController();
    searches.set(host, { query, controller });
    request("?mode=person&search=" + encodeURIComponent(query), undefined, controller.signal).then((result) => {
      if (controller.signal.aborted || !result.contacts.length) return;
      const contacts = result.contacts.slice(0, 8);
      host.innerHTML = `<div class="act-search-heading">ACT! contacts <small>No matching CRD</small></div>
        ${contacts.map((contact, i) => `<button type="button" class="act-search-result" data-act-hit="${i}">
          <b>${esc(contact.name)}</b><span>${esc([contact.company, place(contact.primary)].filter(Boolean).join(" / "))}</span>
        </button>`).join("")}`;
      host.onclick = (event) => {
        const button = event.target.closest("[data-act-hit]");
        if (!button) return;
        const contact = contacts[Number(button.dataset.actHit)];
        if (!contact) return;
        onSelect();
        contactDetails(contact);
      };
      host.hidden = false;
      onChange();
    }).catch(() => {
      // The advisor index remains usable while the authenticated directory is unavailable.
      if (searches.get(host)?.controller === controller) searches.delete(host);
    });
  }

  function scan(node) {
    if (node?.nodeType !== 1) return;
    if (node.matches("[data-act-profile]")) mount(node);
    node.querySelectorAll("[data-act-profile]").forEach(mount);
  }
  root.ActAddresses = { slot, officeSlot, badgeSlot, render, searchInto,
    isContactKey, current, snapshot, loadListContacts, openByKey, contactDetails,
    refreshAssetDisplay, setAssetBook: callback => { assetBook = callback; },
    setLocator: (callback, firmCallback) => { locate = callback; openFirm = firmCallback; } };
  if (root.document && root.MutationObserver) {
    const start = () => {
      scan(root.document.body);
      new root.MutationObserver((changes) => changes.forEach((change) => change.addedNodes.forEach(scan)))
        .observe(root.document.body, { childList: true, subtree: true });
    };
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", start);
    else start();
  }
})(typeof window !== "undefined" ? window : globalThis);
