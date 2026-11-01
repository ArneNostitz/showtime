/**
 * Fullscreen VidSrc player with availability check.
 *
 * Every link marked with `data-vidsrc-play` is checked against the VidSrc info
 * API before it is shown: the play button only appears when VidSrc has the
 * title (or the check could not be completed), otherwise it is replaced by a
 * "No media yet" label. Episodes are resolved from a single show lookup
 * (`/info/tv/{id}.json` lists the available episode range per season), with a
 * per-episode lookup only for seasons that have gaps. Each configured domain
 * (read from the `vidsrc-domains` JSON script) is tried in order and the first
 * reachable mirror is used.
 *
 * Clicking a link opens its embed URL in a full-viewport overlay and requests
 * fullscreen within the same click, so the browser grants it. The iframe
 * delegates autoplay, letting playback start with sound.
 *
 * Popup ads: VidSrc refuses to load inside `<iframe sandbox>`, so popups are
 * blocked by starving them of user activation instead. A click in the player
 * also activates this (ancestor) page, so while the player is open we poll for
 * activation and immediately consume it with a fullscreen request, which
 * consumes it for the whole frame tree. The player's `window.open` calls then
 * fail the popup blocker's activation check, while playback still works since
 * it only needs sticky activation. The fullscreen request doubles as a way to
 * re-enter fullscreen after it was lost.
 *
 * If fullscreen is lost because the page lost focus (e.g. an ad tab slipped
 * through), the player stays open; leaving fullscreen otherwise closes it.
 *
 * The overlay is attached to <html> rather than <body> so it survives the
 * htmx body swap triggered by the episode "mark as watched" request.
 */
(function () {
  "use strict";

  if (window.vidsrcPlayer) {
    window.vidsrcPlayer.scan(document);
    return;
  }

  const PROBE_TIMEOUT_MS = 4000;
  const ACTIVATION_POLL_MS = 5;
  const FOCUS_LOSS_GRACE_MS = 250;
  const MISSING_TEXT = "No media yet";
  const lookups = new Map();
  let overlay = null;
  let wasFullscreen = false;
  let popupGuard = null;

  function getDomains(fallbackHost) {
    const el = document.getElementById("vidsrc-domains");
    let domains = [];
    try {
      domains = el ? JSON.parse(el.textContent) : [];
    } catch (e) {
      domains = [];
    }
    if (!domains.includes(fallbackHost)) domains.unshift(fallbackHost);
    return domains;
  }

  /** Parse an embed URL (path or query format) into its title reference. */
  function parseEmbed(url) {
    const parts = url.pathname.split("/").filter(Boolean);
    const start = parts.indexOf("embed");
    if (start === -1) return null;
    let [type, id, season, episode] = parts.slice(start + 1).map(decodeURIComponent);
    if (!id) {
      id = url.searchParams.get("imdb") || url.searchParams.get("tmdb");
      season = url.searchParams.get("season");
      episode = url.searchParams.get("episode");
    }
    if (!id || (type !== "movie" && type !== "tv")) return null;
    if (type === "tv" && season && episode) {
      return { type, id, season: Number(season), episode: Number(episode) };
    }
    return { type, id };
  }

  function infoPath(ref) {
    const suffix = ref.season !== undefined ? `/${ref.season}/${ref.episode}` : "";
    return `/info/${ref.type}/${encodeURIComponent(ref.id)}${suffix}.json`;
  }

  async function probe(domain, path) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
    try {
      const response = await fetch(`https://${domain}${path}`, {
        credentials: "omit",
        signal: controller.signal,
      });
      if (response.status === 404) return { status: "missing" };
      if (!response.ok) return { status: "error" };
      return { status: "ok", data: await response.json().catch(() => null) };
    } catch (e) {
      return { status: "error" };
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Look a path up on each mirror in turn (memoized).
   * Resolves to `{status: "ok"|"missing"|"unknown", domain, data}`.
   */
  function lookup(host, path) {
    if (!lookups.has(path)) {
      lookups.set(
        path,
        (async () => {
          for (const domain of getDomains(host)) {
            const result = await probe(domain, path);
            if (result.status !== "error") return { ...result, domain };
          }
          lookups.delete(path);
          return { status: "unknown", domain: host };
        })(),
      );
    }
    return lookups.get(path);
  }

  /** Whether an episode is covered by the show info's season ranges, or `null` if unclear. */
  function episodeInShow(data, ref) {
    const seasons = data && Array.isArray(data.seasons) ? data.seasons : null;
    if (!seasons) return null;
    const season = seasons.find((s) => Number(s.season) === ref.season);
    if (!season) return false;
    const first = Number(season.first_episode);
    const last = Number(season.last_episode);
    if (ref.episode < first || ref.episode > last) return false;
    return Number(season.episodes) === last - first + 1 ? true : null;
  }

  /** Resolve a title's availability: `{status, domain}`. */
  async function availability(url) {
    const ref = parseEmbed(url);
    if (!ref) return { status: "unknown", domain: url.host };
    if (ref.season === undefined) return lookup(url.host, infoPath(ref));

    const show = await lookup(url.host, infoPath({ type: ref.type, id: ref.id }));
    if (show.status === "missing") return show;
    if (show.status === "ok") {
      const covered = episodeInShow(show.data, ref);
      if (covered === true) return show;
      if (covered === false) return { status: "missing", domain: show.domain };
    }
    return lookup(url.host, infoPath(ref));
  }

  function markMissing(link) {
    const el = document.createElement("div");
    el.className = link.className;
    el.setAttribute("data-vidsrc-missing", "");
    el.title = "This title is not available on VidSrc yet";
    const label = link.querySelector("[data-vidsrc-label]");
    if (label) {
      label.textContent = MISSING_TEXT;
      el.append(...link.childNodes);
      el.style.cssText = "cursor:default;opacity:.6;";
    } else {
      const badge = document.createElement("span");
      badge.textContent = MISSING_TEXT;
      badge.style.cssText =
        "color:#fff;font:600 13px/1.4 system-ui,sans-serif;padding:4px 10px;border-radius:9999px;background:rgba(0,0,0,.6);";
      el.append(badge);
      el.style.cursor = "default";
    }
    link.replaceWith(el);
  }

  /** Check every unchecked play link under `root`, hiding it until resolved. */
  function scan(root) {
    const scope = root && root.querySelectorAll ? root : document;
    const links = Array.from(scope.querySelectorAll("a[data-vidsrc-play]:not([data-vidsrc-checked])"));
    if (scope.matches && scope.matches("a[data-vidsrc-play]:not([data-vidsrc-checked])")) links.push(scope);
    for (const link of links) {
      link.setAttribute("data-vidsrc-checked", "");
      link.style.visibility = "hidden";
      availability(new URL(link.href, window.location.href)).then((result) => {
        if (result.status === "missing") {
          markMissing(link);
        } else {
          link.style.visibility = "";
        }
      });
    }
  }

  function showMessage(text) {
    if (!overlay) return;
    overlay.querySelector("[data-vidsrc-status]").textContent = text;
  }

  /** Consume user activation as soon as a click in the player grants it. */
  function startPopupGuard(el) {
    stopPopupGuard();
    if (!navigator.userActivation || !el.requestFullscreen || !document.fullscreenEnabled) return;
    popupGuard = setInterval(() => {
      if (!navigator.userActivation.isActive) return;
      el.requestFullscreen({ navigationUI: "hide" }).catch(() => {});
      // A request that leaves activation unconsumed can't guard against popups.
      if (navigator.userActivation.isActive) stopPopupGuard();
    }, ACTIVATION_POLL_MS);
  }

  function stopPopupGuard() {
    if (popupGuard !== null) clearInterval(popupGuard);
    popupGuard = null;
  }

  function close() {
    if (!overlay) return;
    const el = overlay;
    overlay = null;
    wasFullscreen = false;
    stopPopupGuard();
    if (document.fullscreenElement && document.exitFullscreen) {
      document.exitFullscreen().catch(() => {});
    }
    el.remove();
  }

  function buildOverlay() {
    const el = document.createElement("div");
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.setAttribute("aria-label", "Video player");
    el.style.cssText =
      "position:fixed;inset:0;z-index:2147483647;background:#000;display:flex;align-items:center;justify-content:center;";

    const status = document.createElement("div");
    status.setAttribute("data-vidsrc-status", "");
    status.textContent = "Loading…";
    status.style.cssText =
      "color:#9ca3af;font:600 14px/1.5 system-ui,sans-serif;text-align:center;padding:16px;";

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", "Close player");
    closeBtn.textContent = "\u2715";
    closeBtn.style.cssText =
      "position:absolute;top:12px;right:12px;z-index:2;width:40px;height:40px;border:0;border-radius:9999px;background:rgba(0,0,0,.6);color:#fff;font-size:18px;cursor:pointer;";
    closeBtn.addEventListener("click", close);

    el.append(status, closeBtn);
    return el;
  }

  async function open(href) {
    close();
    const url = new URL(href, window.location.href);
    overlay = buildOverlay();
    const current = overlay;
    document.documentElement.appendChild(current);

    if (current.requestFullscreen) {
      current.requestFullscreen({ navigationUI: "hide" }).catch(() => {});
    }

    const result = await availability(url);
    if (overlay !== current) return;
    if (result.status === "missing") {
      showMessage("This title is not available on VidSrc yet.");
      return;
    }
    url.host = result.domain;

    const iframe = document.createElement("iframe");
    iframe.src = url.toString();
    iframe.setAttribute("allow", "autoplay; fullscreen; picture-in-picture; encrypted-media");
    iframe.setAttribute("allowfullscreen", "");
    iframe.setAttribute("referrerpolicy", "origin");
    iframe.style.cssText = "position:absolute;inset:0;width:100%;height:100%;border:0;background:#000;";
    current.insertBefore(iframe, current.firstChild);
    startPopupGuard(current);
    iframe.focus();
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest && event.target.closest("a[data-vidsrc-play]");
    if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    open(link.href);
  });

  document.addEventListener("fullscreenchange", () => {
    if (!overlay) return;
    if (document.fullscreenElement) {
      wasFullscreen = true;
    } else if (wasFullscreen) {
      const current = overlay;
      setTimeout(() => {
        if (overlay === current && !document.fullscreenElement && !document.hidden && document.hasFocus()) {
          close();
        }
      }, FOCUS_LOSS_GRACE_MS);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (overlay && event.key === "Escape" && !document.fullscreenElement) close();
  });

  document.addEventListener("htmx:load", (event) => scan(event.detail && event.detail.elt));

  window.vidsrcPlayer = { open, close, scan };
  scan(document);
})();
