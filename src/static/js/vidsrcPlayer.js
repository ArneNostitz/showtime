/**
 * Fullscreen VidSrc player.
 *
 * Clicking a link marked with `data-vidsrc-play` opens its embed URL in a
 * full-viewport overlay and requests fullscreen within the same click, so the
 * browser grants it. The iframe delegates autoplay, letting playback start
 * with sound. Before loading, the title is looked up on the VidSrc info API
 * of each configured domain (read from the `vidsrc-domains` JSON script) and
 * the first reachable mirror is used.
 *
 * The overlay is attached to <html> rather than <body> so it survives the
 * htmx body swap triggered by the episode "mark as watched" request.
 */
(function () {
  "use strict";

  const PROBE_TIMEOUT_MS = 4000;
  let overlay = null;
  let wasFullscreen = false;

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

  function infoPath(url) {
    const type = url.pathname.split("/").filter(Boolean).pop();
    const id = url.searchParams.get("imdb") || url.searchParams.get("tmdb");
    if (!id || (type !== "movie" && type !== "tv")) return null;
    const season = url.searchParams.get("season");
    const episode = url.searchParams.get("episode");
    const suffix = type === "tv" && season && episode ? `/${season}/${episode}` : "";
    return `/info/${type}/${encodeURIComponent(id)}${suffix}.json`;
  }

  async function probe(domain, path) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
    try {
      const response = await fetch(`https://${domain}${path}`, {
        credentials: "omit",
        cache: "no-store",
        signal: controller.signal,
      });
      if (response.status === 404) return "missing";
      return response.ok ? "ok" : "error";
    } catch (e) {
      return "error";
    } finally {
      clearTimeout(timer);
    }
  }

  /** Resolve to the first mirror that answers, `null` if the title is missing. */
  async function resolveDomain(url) {
    const domains = getDomains(url.host);
    const path = infoPath(url);
    if (!path) return url.host;
    for (const domain of domains) {
      const result = await probe(domain, path);
      if (result === "ok") return domain;
      if (result === "missing") return null;
    }
    return url.host;
  }

  function showMessage(text) {
    if (!overlay) return;
    overlay.querySelector("[data-vidsrc-status]").textContent = text;
  }

  function close() {
    if (!overlay) return;
    const el = overlay;
    overlay = null;
    wasFullscreen = false;
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

    const domain = await resolveDomain(url);
    if (overlay !== current) return;
    if (domain === null) {
      showMessage("This title is not available on VidSrc yet.");
      return;
    }
    url.host = domain;

    const iframe = document.createElement("iframe");
    iframe.src = url.toString();
    iframe.setAttribute("allow", "autoplay; fullscreen; picture-in-picture; encrypted-media");
    iframe.setAttribute("allowfullscreen", "");
    iframe.setAttribute("referrerpolicy", "origin");
    iframe.style.cssText = "position:absolute;inset:0;width:100%;height:100%;border:0;background:#000;";
    current.insertBefore(iframe, current.firstChild);
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
      close();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (overlay && event.key === "Escape" && !document.fullscreenElement) close();
  });

  window.vidsrcPlayer = { open, close };
})();
