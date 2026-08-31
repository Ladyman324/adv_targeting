/* Service worker for the field view — SHELL ONLY, deliberately.
 *
 * WHAT IS CACHED
 *   field.html, field.js, field.css, the manifest and the icons. None of it is
 *   sensitive; all of it is what makes the app open instantly from the home
 *   screen instead of re-downloading itself.
 *
 * WHAT THE SERVICE WORKER NEVER CACHES, AND WHY
 *   /data/*  — never enters Cache Storage or an offline response path. Field
 *              tiles carry names, direct lines, email addresses and ownership;
 *              replaying them offline would bypass authentication and access
 *              revocation. The ordinary browser HTTP cache is separate: field,
 *              contact and ACT payloads use `no-cache` and revalidate, while
 *              only the public desktop pin/national files may be fresh for at
 *              most five minutes. This worker does not handle either policy.
 *   /.auth/* — the sign-in endpoints. Caching an auth response is how a stale
 *              session appears valid.
 *
 * So every field launch still reaches the network or revalidates before field
 * data is reused, which gives Entra a chance to reject an expired/revoked
 * session. The app does not work in a dead zone -- accepted, because a rep with
 * no signal cannot make the call or send the email that the data is for.
 *
 * VERSION is rewritten by web_assets.py from a hash of the shell files, so a
 * deploy invalidates the cache. Never edit it by hand.
 */
const VERSION = "75a01cd733";
const CACHE = `field-shell-${VERSION}`;

const SHELL = [
  "field.html",
  "field.css?v=" + VERSION,
  "field.js?v=" + VERSION,
  "manifest.webmanifest",
  "icon-192.png",
  "icon-512.png",
];

self.addEventListener("install", (e) => {
  // Individually, not addAll: one 404 in the list would reject the whole
  // install and leave the app with no worker at all, silently.
  e.waitUntil(caches.open(CACHE).then((c) =>
    Promise.all(SHELL.map((u) => c.add(u).catch(() => null)))
  ).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k.startsWith("field-shell-") && k !== CACHE)
            .map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // Anything that is not a plain same-origin GET is none of this worker's
  // business -- including every auth redirect and every POST the /api/log
  // endpoint will eventually make.
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/.auth/") || url.pathname.startsWith("/api/")) return;

  // Never put data in service-worker Cache Storage or an offline response path.
  if (url.pathname.startsWith("/data/")) return;

  // The desktop map is out of scope: this worker exists for the field view and
  // should not quietly become responsible for a 4,200-line application it was
  // never tested against.
  const file = url.pathname.split("/").pop() || "";
  const isShell = ["field.html", "field.css", "field.js", "manifest.webmanifest",
                   "icon-192.png", "icon-512.png"].includes(file);
  if (!isShell) return;

  // The document goes to the network first so a new deploy — and an expired
  // session, which arrives as a redirect — is always seen. Cache is the
  // fallback, not the default.
  if (e.request.mode === "navigate" || file === "field.html") {
    e.respondWith(
      fetch(e.request)
        .then((r) => {
          // ONLY CACHE THE REAL APP.
          //
          // This used to cache whatever came back. When the Entra session
          // lapsed, the request was redirected to the Microsoft sign-in page,
          // which returns 200 with HTML -- so the LOGIN PAGE was stored as
          // field.html, and every later load served a login screen dressed as
          // the app until someone force-reloaded. A rep who put their phone
          // down for an hour came back to a broken app.
          //
          // `redirected` is the tell: our own page is served directly, an
          // expired session is not.
          const type = r.headers.get("Content-Type") || "";
          if (r.ok && !r.redirected && type.includes("text/html")) {
            const copy = r.clone();
            caches.open(CACHE).then((c) => c.put(e.request, copy));
          }
          return r;
        })
        .catch(() => caches.match(e.request).then((m) => m || caches.match("field.html")))
    );
    return;
  }

  // Static assets carry ?v=<hash>, so a cache hit can only ever be the right
  // file: a new build produces a new URL rather than a stale answer.
  e.respondWith(
    caches.match(e.request).then((m) => m || fetch(e.request).then((r) => {
      if (r.ok) {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
      }
      return r;
    }))
  );
});
