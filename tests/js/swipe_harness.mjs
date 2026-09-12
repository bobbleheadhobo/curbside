// Drives the card swipe gestures against a minimal fake DOM.
//
// The risky parts are not the pretty bits: it must NOT steal a vertical
// scroll, must NOT commit on a short drag, must NOT act in a direction whose
// button is absent, and must swallow the click that a touch ends in -- letting
// go over the title would otherwise open the marketplace.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");

const out = [];
const eq = (label, got, want) => out.push(
  `${JSON.stringify(got) === JSON.stringify(want) ? "ok" : "FAIL"} ${label} ` +
  `got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {};
    this.listeners = {}; this._cls = new Set(); this.style = new Style();
    this._text = ""; this.value = ""; this.offsetHeight = 100;
  }
  get classList() {
    const s = this._cls;
    return { add: (...c) => c.forEach(x => s.add(x)),
             remove: (...c) => c.forEach(x => s.delete(x)),
             contains: c => s.has(c) };
  }
  get className() { return [...this._cls].join(" "); }
  set className(v) { this._cls = new Set(v.split(" ").filter(Boolean)); }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  insertBefore(n) { this.children.unshift(n); n.parentNode = this; return n; }
  removeChild(c) { this.children = this.children.filter(x => x !== c); }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  addEventListener(k, fn) { (this.listeners[k] ||= []).push(fn); }
  removeEventListener(k, fn) {
    this.listeners[k] = (this.listeners[k] || []).filter(f => f !== fn);
  }
  fire(k, ev = {}) { (this.listeners[k] || []).slice().forEach(f => f(ev)); }
  closest(sel) {
    let n = this;
    while (n) {
      if (sel === "article.card" && n.tagName === "ARTICLE" && n._cls.has("card")) return n;
      n = n.parentNode;
    }
    return null;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const hits = [];
    const walk = n => {
      if (sel === ".actions form [name=status]" &&
          n.attrs.name === "status") hits.push(n);
      if (sel === ".swipehint" && n._cls.has("swipehint")) hits.push(n);
      n.children.forEach(walk);
    };
    this.children.forEach(walk);
    return hits;
  }
  set innerHTML(v) { this._text = v; }
  get innerHTML() { return this._text; }
  set textContent(v) { this._text = v; this.children = []; }
  get textContent() { return this._text; }
}
class Style {
  constructor() { this.props = {}; }
  setProperty(k, v) { this.props[k] = v; }
  getPropertyValue(k) { return this.props[k] || ""; }
  removeProperty(k) { delete this.props[k]; }
}

function makeCard(statuses) {
  const card = new El("article");
  card.className = "card";
  const main = new El("div"); main.className = "card-main";
  const h2 = new El("h2"); const title = new El("a"); h2.appendChild(title);
  main.appendChild(h2); card.appendChild(main);
  const actions = new El("div"); actions.className = "actions";
  statuses.forEach(v => {
    const form = new El("form");
    const input = new El("input"); input.setAttribute("name", "status");
    input.attrs.name = "status"; input.value = v;
    form.appendChild(input); actions.appendChild(form);
  });
  card.appendChild(actions);
  // A real card always has a parent: `leave()` remembers it so undo can put
  // the card back exactly where it was.
  const stack = new El("div"); stack.className = "stack";
  stack.appendChild(card);
  return { card, title, stack };
}

const posted = [];
const docListeners = {};
global.document = {
  addEventListener: (k, fn) => (docListeners[k] ||= []).push(fn),
  getElementById: () => null,          // the pill editor bows out
  querySelector: () => null,
  createElement: t => new El(t),
  createTextNode: t => ({ textContent: t }),
  body: new El("body"),
};
// NOT reduced motion: the animated path is the one with timing to get wrong.
// Under prefers-reduced-motion every cleanup collapses to 0ms, which is right
// but also skips straight past the states worth asserting on.
global.window = { matchMedia: () => ({ matches: false }) };
global.addEventListener = () => {};
global.fetch = (url, opts) => {
  posted.push({ url, body: opts && opts.body });
  return Promise.resolve({ ok: true, status: 200,
                           json: () => Promise.resolve({ ok: true }),
                           headers: { get: () => "" } });
};
global.FormData = class { constructor() {} get() { return "x"; } };
global.URLSearchParams = class { constructor(v) { this.v = v; } };
global.DOMParser = class { parseFromString() { return { querySelector: () => null }; } };
// Short timers (the fold-away animation, the toast's own removal) run at
// once; long ones -- the 7s auto-dismiss -- stay pending, or the toast would
// close itself before a test could swipe it.
// Short timers (the fold-away, the toast's own removal) run at once. Longer
// ones queue: the 7s auto-dismiss must not close a toast before a test can
// swipe it, and the 700ms click-swallow expiry has to be triggerable on
// purpose -- that expiry IS the fix for a listener that used to eat the next
// real tap, so a harness that fired it eagerly would hide both the bug and
// the fix.
const pending = [];
global.setTimeout = (fn, ms) => {
  if ((ms || 0) < 500) { fn(); return 0; }
  pending.push(fn);
  return pending.length;
};
const flushTimers = () => pending.splice(0).forEach(f => f());
global.clearTimeout = () => {};
global.requestAnimationFrame = (fn) => { fn(); return 0; };

eval(src);

const fire = (k, ev) => (docListeners[k] || []).forEach(f => f(ev));
const touch = (x, y) => ({ touches: [{ clientX: x, clientY: y }],
                           cancelable: true, preventDefault() {} });
const sx = c => c.style.getPropertyValue("--sx");

// --- a scroll is a scroll, and must be left alone ---------------------------
// DIAGONAL on purpose. A straight vertical drag proves nothing: its dx never
// reaches the slop threshold, so it would be ignored even with the axis test
// deleted. A thumb going down a list travels sideways too, and THAT is the
// drag that must not turn into a swipe.
let { card } = makeCard(["saved", "dismissed"]);
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(220, 260));          // dx 20 (over slop), dy 60
eq("a diagonal scroll never engages", card.classList.contains("swiping"), false);
eq("... and never moves the card", sx(card), "");
fire("touchend", {});

// ... and once it is a scroll it stays one, even if the thumb drifts sideways
({ card } = makeCard(["saved", "dismissed"]));
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(220, 260));
fire("touchmove", touch(320, 262));          // now strongly horizontal
eq("a scroll cannot become a swipe mid-drag",
   card.classList.contains("swiping"), false);
fire("touchend", {});

// --- a short horizontal drag moves but does not commit ----------------------
posted.length = 0;
({ card } = makeCard(["saved", "dismissed"]));
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(230, 202));
eq("a horizontal drag engages", card.classList.contains("swiping"), true);
eq("... and follows the finger", sx(card), "30px");
fire("touchend", {});
eq("a short drag commits nothing", posted.length, 0);

// --- past the threshold, right saves ---------------------------------------
posted.length = 0;
({ card } = makeCard(["saved", "dismissed"]));
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(300, 205));
const h = card.querySelector(".swipehint");
eq("the hint names the action", h && h.className, "swipehint saved");
fire("touchend", {});
eq("a long right swipe saves", posted.map(p => p.url), ["/triage"]);

// --- left dismisses ---------------------------------------------------------
posted.length = 0;
({ card } = makeCard(["saved", "dismissed"]));
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(100, 205));
eq("the hint flips direction",
   card.querySelector(".swipehint").className, "swipehint dismissed");
fire("touchend", {});
eq("a long left swipe dismisses", posted.map(p => p.url), ["/triage"]);

// --- a direction with no button must not act --------------------------------
posted.length = 0;
({ card } = makeCard(["dismissed"]));            // this is /saved
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(320, 205));              // swipe right, no Save button
eq("no hint where there is no action", card.querySelector(".swipehint"), null);
eq("... and it barely moves", sx(card), "24px");
fire("touchend", {});
eq("... and nothing is posted", posted.length, 0);

// --- the click a touch ends in must not open the marketplace ----------------
let t2;
({ card, title: t2 } = makeCard(["saved", "dismissed"]));
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(300, 205));
fire("touchend", {});
let defaulted = false;
card.fire("click", { preventDefault() { defaulted = true; }, stopPropagation() {} });
eq("the click after a swipe is swallowed", defaulted, true);
defaulted = false;
card.fire("click", { preventDefault() { defaulted = true; }, stopPropagation() {} });
eq("... but only once", defaulted, false);

// --- a card with no triage buttons is inert ---------------------------------
posted.length = 0;
({ card } = makeCard([]));                        // a hunt view
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(320, 205));
eq("a card with no actions never engages", card.classList.contains("swiping"), false);

// --- the toast can be swiped away ------------------------------------------
// It sits over the list for seven seconds holding an Undo you usually do not
// want. Swiping it must dismiss it WITHOUT undoing, and must not press Undo.
const body = global.document.body;
// `act()` raises the toast inside a promise callback, so it does not exist
// until the microtask queue drains. Top-level await, this being an ES module.
const tick = async () => { for (let i = 0; i < 4; i++) await Promise.resolve(); };
// Not simply the last child: `announce()` appends its own live region after
// the toast, so pick by class.
const lastToast = () =>
  body.children.filter(c => c._cls.has("toast")).slice(-1)[0];

function swipeToast(dx, dy) {
  const el = lastToast();
  el.fire("touchstart", { touches: [{ clientX: 200, clientY: 600 }],
                          cancelable: true, preventDefault() {} });
  el.fire("touchmove", { touches: [{ clientX: 200 + dx, clientY: 600 + dy }],
                         cancelable: true, preventDefault() {} });
  el.fire("touchend", {});
  return el;
}

posted.length = 0;
body.children.length = 0;
({ card } = makeCard(["saved", "dismissed"]));
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(300, 205));
fire("touchend", {});                              // saves -> raises a toast
await tick();
eq("triaging raises a toast", !!lastToast(), true);
eq("... with an Undo", lastToast().children.length, 2);

// a short drag springs back
let toastEl = swipeToast(20, 0);
eq("a short drag keeps the toast", body.children.indexOf(toastEl) >= 0, true);
eq("... and clears the offset", toastEl.style.getPropertyValue("--tx"), "");

// sideways past the threshold dismisses it
const before = posted.length;
toastEl = swipeToast(120, 0);
eq("a sideways flick dismisses the toast",
   body.children.indexOf(toastEl) >= 0, false);
eq("... and never undoes anything", posted.length, before);

// downward dismisses too
posted.length = 0;
body.children.length = 0;
({ card } = makeCard(["saved", "dismissed"]));
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(300, 205));
fire("touchend", {});
await tick();
toastEl = swipeToast(0, 110);
eq("a downward flick dismisses it too",
   body.children.indexOf(toastEl) >= 0, false);

// upward resists: nothing up there to go to
posted.length = 0;
body.children.length = 0;
({ card } = makeCard(["saved", "dismissed"]));
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(300, 205));
fire("touchend", {});
await tick();
toastEl = swipeToast(0, -200);                     // damped to -50
eq("an upward drag never dismisses it",
   body.children.indexOf(toastEl) >= 0, true);

// and the click a swipe ends in must not press Undo
let undone = false;
toastEl.fire("click", { preventDefault() { undone = true; },
                        stopPropagation() {} });
eq("the click after a toast swipe is swallowed", undone, true);

// --- regressions found auditing the gestures --------------------------------

// A drag towards an action the card does not have is still a DRAG. It used not
// to be recorded as movement, so the click was not swallowed and letting go
// over the title opened the marketplace.
({ card } = makeCard(["dismissed"]));                 // /saved: no Save
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(320, 205));                   // right, into the void
fire("touchend", {});
let opened = false;
card.fire("click", { preventDefault() { opened = true; }, stopPropagation() {} });
eq("a dead-direction drag still swallows its click", opened, true);

// The swallow must EXPIRE. A drag usually suppresses the click by itself, so
// the listener sat waiting and ate the next real tap on that card instead.
({ card } = makeCard(["saved", "dismissed"]));
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(230, 202));                   // short: springs back
fire("touchend", {});
flushTimers();                                        // 700ms passes
opened = false;
card.fire("click", { preventDefault() { opened = true; }, stopPropagation() {} });
eq("the swallow expires, so a later tap still works", opened, false);

// Committing flings the card out rather than yanking it home and folding from
// the middle -- and leaves NO inline offset, which is what would strand a card
// off-screen when undo puts it back.
let stack;
({ card, stack } = makeCard(["saved", "dismissed"]));
fire("touchstart", { ...touch(200, 200), target: card });
fire("touchmove", touch(300, 205));
fire("touchend", {});
eq("a committed swipe flings", card.classList.contains("flinging"), true);
eq("... with no inline offset left behind", sx(card), "0px");

// ... so undo brings it back on-screen. The fling offset hangs off the status
// class, which `restore()` removes; an inline one would survive and the card
// would return invisible, somewhere off to the right.
await tick();
lastToast().children[1].fire("click");                // Undo
await tick();
eq("undo puts the card back", stack.children.indexOf(card) >= 0, true);
eq("... and the fling offset goes with the status class",
   card.classList.contains("saved"), false);
eq("... leaving it where it belongs", sx(card), "0px");

console.log(out.join("\n"));
if (out.some(l => l.startsWith("FAIL"))) process.exit(1);
