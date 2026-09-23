/* Shared onboarding and internal training player for desktop and Field App.
 * No media is fetched during startup. */
(function trainingModule(global) {
  "use strict";
  const VERSION = "1";
  const API = "/api/training-video";
  const CHAPTERS = [
    [0, "Getting connected and setting preferences"],
    [83, "Explore, filter, and select areas"],
    [283, "Firms, buildings, teams, and contact roles"],
    [385, "Build and manage outreach lists"],
    [500, "Call a list"],
    [567, "Email a list and select materials"],
    [707, "Personalize and review emails"],
    [846, "Approve, schedule, and send safely"],
    [911, "Use the Field App"],
  ];
  let active = null;
  let promptOffered = false;
  let savingSeen = null;

  function settingsCard() {
    return `<div class="set-row set-block training-setting">`
      + `<span>Help &amp; training</span>`
      + `<p class="set-sub">A 16-minute introduction to finding advisors, building lists, `
      + `and using the map in the field.</p>`
      + `<p class="set-actions"><button type="button" class="set-btn" `
      + `data-training-video="open">Watch introduction</button></p></div>`;
  }

  function close(back) {
    const target = back || active;
    if (!target) return;
    const video = target.querySelector("video");
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    if (target._trainingKey) document.removeEventListener("keydown", target._trainingKey);
    target.remove();
    if (active === target) active = null;
  }

  function mount(inner, label) {
    if (active) close(active);
    const back = document.createElement("div");
    back.className = "ask-back training-back";
    back.innerHTML = inner;
    back.addEventListener("click", (event) => {
      if (event.target === back || event.target.closest("[data-training-close]")) close(back);
    });
    back._trainingKey = (event) => {
      if (event.key === "Escape") close(back);
    };
    document.addEventListener("keydown", back._trainingKey);
    document.body.appendChild(back);
    active = back;
    const first = back.querySelector("button, video, a[href]");
    if (first) first.focus();
    back.setAttribute("aria-label", label);
    return back;
  }

  async function rememberSeen(version = VERSION) {
    const value = String(version || VERSION);
    if (!global.Dial || Dial.setting("introVideoSeenVersion") === value) return;
    if (!savingSeen) {
      savingSeen = Dial.saveSettings({ introVideoSeenVersion: value })
        .catch(() => {})
        .finally(() => { savingSeen = null; });
    }
    return savingSeen;
  }
  async function open() {
    const back = mount(
      `<div class="ask training-dialog" role="dialog" aria-modal="true" `
        + `aria-labelledby="trainingTitle">`
        + `<button type="button" class="training-close" data-training-close `
        + `aria-label="Close video">&times;</button>`
        + `<h3 id="trainingTitle">Advisor Map introduction</h3>`
        + `<p class="training-meta">16 minutes &middot; Internal training</p>`
        + `<div class="training-stage" aria-live="polite">`
        + `<p class="training-loading">Preparing the video&hellip;</p></div>`
        + `<button type="button" class="ask-btn ghost" data-training-close>Close</button>`
        + `</div>`,
      "Advisor Map introduction video");
    const stage = back.querySelector(".training-stage");

    try {
      const response = await fetch(API, { credentials: "same-origin", cache: "no-store" });
      let data = null;
      try { data = await response.json(); } catch { /* named below */ }
      if (!response.ok || !data || !data.videoUrl) {
        throw new Error((data && data.error) || "The introduction could not be loaded.");
      }
      if (!back.isConnected) return;
      showPlayer(back, stage, data);
    } catch (error) {
      if (!back.isConnected) return;
      stage.replaceChildren();
      const note = document.createElement("p");
      note.className = "training-error";
      note.textContent = error.message || "The introduction could not be loaded.";
      stage.appendChild(note);
    }
  }

  function showPlayer(back, stage, data) {
    back.querySelector("#trainingTitle").textContent = data.title || "Advisor Map introduction";
    const video = document.createElement("video");
    video.className = "training-video";
    video.controls = true;
    video.playsInline = true;
    video.preload = "metadata";
    video.crossOrigin = "anonymous";
    if (data.posterUrl) video.poster = data.posterUrl;
    video.src = data.videoUrl;
    video.addEventListener("play", () => rememberSeen(data.version || VERSION), { once: true });
    if (data.captionsUrl) {
      const track = document.createElement("track");
      track.kind = "captions";
      track.label = "English";
      track.srclang = "en";
      track.src = data.captionsUrl;
      track.default = true;
      video.appendChild(track);
    }
    stage.replaceChildren(video);

    const error = document.createElement("p");
    error.className = "training-error";
    error.hidden = true;
    error.textContent = "Playback was interrupted. Close this window and try again for a fresh link.";
    video.addEventListener("error", () => { error.hidden = false; });
    stage.appendChild(error);

    const separate = document.createElement("a");
    separate.className = "training-open-link";
    separate.href = data.videoUrl;
    separate.target = "_blank";
    separate.rel = "noopener";
    separate.textContent = "Open video in a new tab";
    stage.appendChild(separate);
    stage.appendChild(chapterList(video));
  }

  function chapterList(video) {
    const details = document.createElement("details");
    details.className = "training-chapters";
    const summary = document.createElement("summary");
    summary.textContent = "Jump to a section";
    details.appendChild(summary);
    const list = document.createElement("div");
    for (const [seconds, label] of CHAPTERS) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}  ${label}`;
      button.addEventListener("click", () => {
        video.currentTime = seconds;
        video.play().catch(() => {});
      });
      list.appendChild(button);
    }
    details.appendChild(list);
    return details;
  }
  function maybePrompt() {
    if (promptOffered || !global.Dial
        || Dial.setting("introVideoSeenVersion") === VERSION) return;
    promptOffered = true;
    global.setTimeout(() => {
      // Never stack onboarding over a task the rep has already opened.
      if (document.querySelector(".ask-back") || document.hidden) return;
      const back = mount(
        `<div class="ask training-welcome" role="dialog" aria-modal="true" `
          + `aria-labelledby="trainingWelcomeTitle">`
          + `<p class="training-eyebrow">Getting started</p>`
          + `<h3 id="trainingWelcomeTitle">New to the Advisor Map?</h3>`
          + `<p>Watch the 16-minute introduction to the map, lists, email tools, `
          + `and Field App. You can always find it later in Settings.</p>`
          + `<button type="button" class="ask-btn primary" data-training-video="welcome">`
          + `Watch introduction</button>`
          + `<button type="button" class="ask-btn ghost" data-training-video="dismiss">Not now</button>`
          + `</div>`,
        "Advisor Map introduction invitation");
      wirePrompt(back);
    }, 1200);
  }

  function wirePrompt(back) {
    back.querySelector('[data-training-video="welcome"]').addEventListener("click", () => {
      close(back);
      open();
    });
    back.querySelector('[data-training-video="dismiss"]').addEventListener("click", () => {
      rememberSeen(VERSION);
      close(back);
    });
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest('[data-training-video="open"]');
    if (button) open();
  });
  global.TrainingVideo = { VERSION, settingsCard, open, maybePrompt };
})(window);
