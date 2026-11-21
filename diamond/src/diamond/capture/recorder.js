/*
 * Diamond in-page recorder.
 *
 * Injected before any page script runs. It observes, never modifies, the page
 * (the only DOM it adds is a closed-shadow REC badge that is excluded from all
 * measurements). Events are batched and sent to the engine through the
 * `__diamondEmit` binding that Playwright exposes.
 *
 * Every timestamp is epoch milliseconds (performance.timeOrigin + now), the
 * same clock the audio recorder uses, so speech and pointer can be aligned.
 */
(() => {
  if (window.__diamondRecorder) return;
  // Only the page the reviewer looks at. Init scripts also run in every iframe
  // (ads, captchas, 1x1 trackers) whose events would pollute the analysis.
  try { if (window.top !== window) return; } catch (_) { return; }
  // The blank tab that exists before the first navigation is not part of the app.
  if (!/^(https?|file):$/.test(location.protocol)) return;
  window.__diamondRecorder = true;

  const CFG = Object.assign(
    { dwellMs: 700, sourceAttributes: ['data-insp-path', 'data-source', 'data-diamond-src'], maxInventory: 600 },
    window.__diamondConfig || {}
  );
  const OVERLAY_ID = 'diamond-overlay';
  const now = () => performance.timeOrigin + performance.now();
  const queue = [];
  const emit = (type, data) => queue.push(Object.assign({ type, ts: now(), url: location.href }, data));

  const flush = () => {
    if (!queue.length || typeof window.__diamondEmit !== 'function') return;
    const batch = queue.splice(0, queue.length);
    try { window.__diamondEmit(batch); } catch (_) { /* page closing */ }
  };
  setInterval(flush, 250);
  addEventListener('pagehide', flush);

  // ---------------------------------------------------------------- helpers
  const isOverlay = (el) => !!(el && el.closest && el.closest('#' + OVERLAY_ID));
  const cssEscape = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/[^a-zA-Z0-9_-]/g, '\\$&'));
  const looksGenerated = (s) => /\d{4,}|^[a-f0-9]{6,}$|^(css|sc|jsx|emotion)-/i.test(s);

  function selectorOf(el) {
    if (!(el instanceof Element)) return '';
    const parts = [];
    let node = el;
    for (let depth = 0; node && node.nodeType === 1 && depth < 6; depth++) {
      if (node.id && !looksGenerated(node.id)) {
        parts.unshift('#' + cssEscape(node.id));
        break;
      }
      const testId = node.getAttribute('data-testid') || node.getAttribute('data-test');
      if (testId) {
        parts.unshift(`[data-testid="${testId}"]`);
        break;
      }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = [...parent.children].filter((c) => c.tagName === node.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  }

  function parseColor(c) {
    const m = (c || '').match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }

  // What is painted behind the element:
  //   'rgb(r, g, b)'                                 a solid colour
  //   'gradient:rgba(..)|rgba(..)@rgb(r, g, b)'      CSS gradient stops over the solid colour below
  //   'image'                                        a picture (contrast cannot be computed)
  function effectiveBackground(el) {
    let stops = null;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n);
      const c = parseColor(cs.backgroundColor);
      if (c && c.a > 0.5) {
        const base = `rgb(${c.r}, ${c.g}, ${c.b})`;
        return stops ? `gradient:${stops.join('|')}@${base}` : base;
      }
      const img = cs.backgroundImage;
      if (img && img !== 'none' && !stops) {
        if (/url\(/.test(img) || !/gradient\(/.test(img)) return 'image';
        stops = img.match(/rgba?\([^)]*\)/g) || [];
        if (!stops.length) return 'image';
      }
    }
    const white = 'rgb(255, 255, 255)';
    return stops ? `gradient:${stops.join('|')}@${white}` : white;
  }

  function sourceHints(el) {
    const hints = {};
    for (let n = el, depth = 0; n && n.nodeType === 1 && depth < 8; n = n.parentElement, depth++) {
      for (const attr of CFG.sourceAttributes) {
        const v = n.getAttribute(attr);
        if (v && !hints.attr) hints.attr = { name: attr, value: v, depth };
      }
      if (hints.attr) break;
    }
    // React ≤18 dev builds: fiber._debugSource (removed in React 19).
    const fiberKey = Object.keys(el).find((k) => k.startsWith('__reactFiber$'));
    if (fiberKey) {
      let f = el[fiberKey];
      for (let i = 0; f && i < 25; i++, f = f.return) {
        if (!hints.component && f.type && typeof f.type === 'function') hints.component = f.type.displayName || f.type.name;
        if (f._debugSource && !hints.react) {
          hints.react = { file: f._debugSource.fileName, line: f._debugSource.lineNumber, column: f._debugSource.columnNumber };
        }
        if (hints.component && hints.react) break;
      }
    }
    const vue3 = el.__vueParentComponent;
    if (vue3 && vue3.type) {
      hints.component = hints.component || vue3.type.name || vue3.type.__name;
      if (vue3.type.__file) hints.vue = { file: vue3.type.__file };
    } else if (el.__vue__ && el.__vue__.$options && el.__vue__.$options.__file) {
      hints.vue = { file: el.__vue__.$options.__file };
    }
    if (el.__svelte_meta && el.__svelte_meta.loc) {
      const l = el.__svelte_meta.loc;
      hints.svelte = { file: l.file, line: l.line + 1, column: l.column };
    }
    return hints;
  }

  const STYLE_KEYS = [
    'color', 'backgroundColor', 'borderColor', 'borderRadius', 'borderWidth', 'fontSize', 'fontFamily', 'fontWeight',
    'lineHeight', 'letterSpacing', 'textAlign', 'direction', 'paddingTop', 'paddingRight', 'paddingBottom',
    'paddingLeft', 'marginTop', 'marginRight', 'marginBottom', 'marginLeft', 'boxShadow', 'cursor', 'opacity',
    'transitionProperty', 'transitionDuration', 'animationName', 'animationDuration', 'display', 'position', 'zIndex',
  ];

  const INTERACTIVE = 'button, a[href], [role=button], [role=link], [role=tab], [role=menuitem], input, select, textarea, summary, [onclick]';

  function accessibleName(el) {
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) {
      const t = labelled.split(/\s+/).map((id) => document.getElementById(id)?.textContent || '').join(' ').trim();
      if (t) return t;
    }
    if (el.labels && el.labels.length) return [...el.labels].map((l) => l.textContent).join(' ').trim();
    return (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title') ||
      el.getAttribute('alt') || el.querySelector?.('img[alt]')?.getAttribute('alt') || '').trim();
  }

  function describe(el, withStyles = true) {
    if (!(el instanceof Element) || isOverlay(el)) return null;
    const r = el.getBoundingClientRect();
    const d = {
      selector: selectorOf(el),
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      text: (el.innerText || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 120),
      name: accessibleName(el).slice(0, 120),
      id: el.id || '',
      classes: typeof el.className === 'string' ? el.className.trim().split(/\s+/).filter(Boolean).slice(0, 8) : [],
      rect: { x: Math.round(r.left), y: Math.round(r.top + scrollY), w: Math.round(r.width), h: Math.round(r.height) },
      interactive: el.matches(INTERACTIVE),
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true' || el.closest('fieldset[disabled]')),
      source: sourceHints(el),
    };
    if (withStyles) {
      const cs = getComputedStyle(el);
      d.styles = {};
      for (const k of STYLE_KEYS) d.styles[k] = cs[k];
      d.styles.effectiveBackground = effectiveBackground(el);
    }
    return d;
  }

  // ------------------------------------------------------- mutation & network
  let mutations = 0;
  let resources = 0;
  new MutationObserver((list) => {
    for (const m of list) if (!isOverlay(m.target)) mutations++;
  }).observe(document, { subtree: true, childList: true, attributes: true, characterData: true });

  const observe = (type, cb, extra = {}) => {
    try {
      new PerformanceObserver((list) => list.getEntries().forEach(cb)).observe(Object.assign({ type, buffered: true }, extra));
    } catch (_) { /* entry type not supported by this browser */ }
  };
  observe('resource', () => resources++);
  observe('longtask', (e) => emit('longtask', { start: performance.timeOrigin + e.startTime, duration: e.duration }));
  observe('long-animation-frame', (e) => emit('loaf', {
    start: performance.timeOrigin + e.startTime,
    duration: e.duration,
    blockingDuration: e.blockingDuration,
    scripts: (e.scripts || []).slice(0, 5).map((s) => ({
      sourceURL: s.sourceURL, sourceFunctionName: s.sourceFunctionName, invoker: s.invoker,
      duration: s.duration, sourceCharPosition: s.sourceCharPosition,
    })),
  }));
  observe('layout-shift', (e) => emit('layout_shift', {
    start: performance.timeOrigin + e.startTime,
    value: e.value,
    hadRecentInput: e.hadRecentInput,
    sources: (e.sources || []).map((s) => (s.node && s.node.nodeType === 1 ? selectorOf(s.node) : '')).filter(Boolean).slice(0, 5),
  }));
  observe('event', (e) => {
    if (e.duration >= 40) {
      emit('event_timing', {
        start: performance.timeOrigin + e.startTime, name: e.name, duration: e.duration,
        processing: e.processingEnd - e.processingStart, target: e.target ? selectorOf(e.target) : '',
      });
    }
  }, { durationThreshold: 40 });

  // ------------------------------------------------------------------ pointer
  let hovered = null;
  let lastMove = { x: 0, y: 0, t: 0 };
  let dwellTimer = null;
  let dwellSent = null;
  let lastTrail = 0;

  addEventListener('mousemove', (ev) => {
    lastMove = { x: ev.clientX, y: ev.clientY, t: now() };
    if (lastMove.t - lastTrail > 100) {
      lastTrail = lastMove.t;
      emit('pointer', { x: ev.clientX, y: ev.clientY + scrollY });
    }
    const el = ev.target;
    if (el !== hovered && !isOverlay(el)) {
      hovered = el;
      emit('hover', { x: ev.clientX, y: ev.clientY + scrollY, el: describe(el, false) });
    }
    clearTimeout(dwellTimer);
    dwellTimer = setTimeout(() => {
      if (hovered && hovered !== dwellSent) {
        dwellSent = hovered;
        emit('dwell', { x: lastMove.x, y: lastMove.y + scrollY, el: describe(hovered, true) });
        flush();
      }
    }, CFG.dwellMs);
  }, { capture: true, passive: true });

  addEventListener('click', (ev) => {
    if (isOverlay(ev.target)) return;
    const el = ev.target.closest(INTERACTIVE) || ev.target;
    const before = { m: mutations, r: resources, href: location.href, focus: document.activeElement };
    const clickTs = now();
    const desc = describe(el, true);
    emit('click', { x: ev.clientX, y: ev.clientY + scrollY, button: ev.button, el: desc });
    setTimeout(() => {
      emit('click_effect', {
        clickTs,
        el: { selector: desc && desc.selector, interactive: desc && desc.interactive, cursor: desc && desc.styles && desc.styles.cursor },
        mutations: mutations - before.m,
        requests: resources - before.r,
        navigated: location.href !== before.href,
        focusChanged: document.activeElement !== before.focus,
      });
    }, 1000);
  }, { capture: true, passive: true });

  let lastScroll = 0;
  addEventListener('scroll', () => {
    const t = now();
    if (t - lastScroll > 250) { lastScroll = t; emit('scroll', { y: scrollY }); }
  }, { capture: true, passive: true });

  // Alt+Shift+M: explicit "look here" mark, useful without a microphone.
  addEventListener('keydown', (ev) => {
    if (ev.altKey && ev.shiftKey && ev.code === 'KeyM') {
      emit('mark', { el: hovered ? describe(hovered, true) : null });
      flush();
    }
  }, { capture: true });

  // ---------------------------------------------------------------- inventory
  const LAYOUT_PROPS = /\b(all|width|height|top|left|right|bottom|margin|padding|max-height|max-width)\b/;

  function inventory(reason) {
    const els = [];
    const seen = new Set();
    const push = (el) => {
      if (seen.has(el) || els.length >= CFG.maxInventory || isOverlay(el)) return;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return;
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) return;
      seen.add(el);
      els.push(describe(el, true));
    };
    document.querySelectorAll(INTERACTIVE).forEach(push);
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n && els.length < CFG.maxInventory; n = walker.nextNode()) {
      if (n.textContent.trim().length > 1 && n.parentElement) push(n.parentElement);
    }
    const images = [...document.images].filter((i) => !isOverlay(i)).slice(0, 100).map((i) => {
      // A hidden <img> without a source is a placeholder waiting for content, not a broken image.
      const pending = i.hidden || !i.getAttribute('src') || getComputedStyle(i).display === 'none';
      return {
        selector: selectorOf(i), src: (i.currentSrc || i.src || '').slice(0, 200), alt: i.getAttribute('alt'),
        pending, broken: !pending && i.complete && i.naturalWidth === 0,
      };
    });
    const animations = [];
    for (const el of document.querySelectorAll('body *')) {
      if (animations.length >= 200) break;
      if (isOverlay(el)) continue;
      const cs = getComputedStyle(el);
      const tDur = Math.max(...cs.transitionDuration.split(',').map(parseFloat)) * 1000;
      const aDur = Math.max(...cs.animationDuration.split(',').map(parseFloat)) * 1000;
      if (tDur > 0 || (cs.animationName !== 'none' && aDur > 0)) {
        animations.push({
          selector: selectorOf(el), transitionProperty: cs.transitionProperty, transitionDurationMs: tDur || 0,
          animationName: cs.animationName, animationDurationMs: aDur || 0,
          layoutBound: tDur > 0 && LAYOUT_PROPS.test(cs.transitionProperty),
          source: sourceHints(el),
        });
      }
    }
    const doc = document.documentElement;
    emit('inventory', {
      reason,
      title: document.title,
      lang: doc.getAttribute('lang') || '',
      dir: (doc.getAttribute('dir') || getComputedStyle(document.body || doc).direction || '').toLowerCase(),
      viewport: { w: innerWidth, h: innerHeight },
      scrollWidth: doc.scrollWidth,
      elements: els,
      images,
      animations,
    });
    flush();
  }

  let invTimer = null;
  const scheduleInventory = (reason, delay = 1200) => {
    clearTimeout(invTimer);
    invTimer = setTimeout(() => inventory(reason), delay);
  };
  const onLoad = () => { emit('page', { title: document.title }); scheduleInventory('load', 800); };
  if (document.readyState === 'complete') onLoad(); else addEventListener('load', onLoad);

  // SPA navigations (Next.js / React Router / Vue Router all use the History API).
  for (const fn of ['pushState', 'replaceState']) {
    const orig = history[fn];
    history[fn] = function (...args) {
      const r = orig.apply(this, args);
      emit('page', { title: document.title, spa: true });
      scheduleInventory('spa-navigation');
      return r;
    };
  }
  addEventListener('popstate', () => { emit('page', { title: document.title, spa: true }); scheduleInventory('spa-navigation'); });
  window.__diamondInventory = () => inventory('manual');

  // ------------------------------------------------------------------- badge
  const mountBadge = () => {
    if (!document.body || document.getElementById(OVERLAY_ID)) return;
    const host = document.createElement('div');
    host.id = OVERLAY_ID;
    host.style.cssText = 'position:fixed;bottom:12px;left:12px;z-index:2147483647;pointer-events:none';
    const root = host.attachShadow({ mode: 'closed' });
    root.innerHTML = '<div style="font:600 11px system-ui;background:#111c;color:#fff;padding:4px 8px;border-radius:999px">' +
      '<span style="color:#ff4d4f">●</span> Diamond REC</div>';
    document.body.appendChild(host);
  };
  if (document.body) mountBadge(); else addEventListener('DOMContentLoaded', mountBadge);
})();
