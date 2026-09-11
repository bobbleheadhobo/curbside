/* Curbside, in the browser.
 *
 * Two jobs: act on a card without reloading the page, and save a settings
 * form without losing your place. Everything here is a progressive
 * enhancement over real <form> posts -- with this file missing, every
 * button still works and simply reloads.
 *
 * Lived inline in base.html, where it could not be linted, syntax-checked
 * or diffed sensibly. tests/test_web.py runs `node --check` over it.
 */
/* Triage without a page reload.
 *
 * Every button below is a real <form> that posts to /triage and works with
 * this script absent -- it just reloads and drops you at the top of the list,
 * which is what made going through twenty listings miserable. Here we post it
 * in the background, show which button you pressed on the card itself, fold
 * the card away and offer an undo.
 *
 * Undo matters more than the animation. Dismissing is not cosmetic: the gate
 * never spends money on that listing again and the title becomes a negative
 * example, so a mis-tap on a phone is expensive and used to be unrecoverable.
 */
(function () {
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var ICON = {saved: "i-saved", dismissed: "i-x", blocked: "i-gone"};
  var LABEL = {saved: "Saved", dismissed: "Dismissed", blocked: "Blocked"};
  var live;

  function post(url, data) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: {"X-Requested-With": "fetch"},
      body: new URLSearchParams(data)
    });
  }

  function announce(text) {
    if (!live) {
      live = document.createElement("div");
      live.className = "sr";
      live.setAttribute("aria-live", "polite");
      document.body.appendChild(live);
    }
    live.textContent = text;
  }

  var current;
  function toast(text, actionLabel, onAction, bad) {
    if (current) { current.remove(); current = null; }
    var el = document.createElement("div");
    el.className = "toast" + (bad ? " bad" : "");
    var span = document.createElement("span");
    span.textContent = text;
    el.appendChild(span);
    if (actionLabel) {
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = actionLabel;
      b.addEventListener("click", function () { close(); onAction(); });
      el.appendChild(b);
    }
    document.body.appendChild(el);
    requestAnimationFrame(function () { el.classList.add("up"); });
    var timer = setTimeout(close, actionLabel ? 7000 : 3400);
    function close() {
      clearTimeout(timer);
      if (current === el) current = null;
      el.classList.remove("up");
      setTimeout(function () { el.remove(); }, 300);
    }
    current = el;
    announce(text);
    return close;
  }

  /* Fold a card away, and hand back a function that puts it exactly where it
     was -- same position, same markup -- so undo is a real undo. */
  function leave(card, kind) {
    var parent = card.parentNode, next = card.nextSibling;
    var badge = document.createElement("div");
    badge.className = "verdict";
    badge.innerHTML = '<svg class="i" aria-hidden="true"><use href="#' +
      ICON[kind] + '"/></svg>' + LABEL[kind];
    card.appendChild(badge);
    card.classList.add("going", kind);
    card.style.maxHeight = card.offsetHeight + "px";

    setTimeout(function () {
      card.classList.add("collapsing");
      setTimeout(function () { card.remove(); emptyCheck(parent); },
                 reduced ? 20 : 280);
    }, reduced ? 10 : 300);

    bump(-1);
    return function restore() {
      card.classList.remove("going", "collapsing", kind);
      card.style.maxHeight = "";
      badge.remove();
      var done = parent.querySelector(".alldone");
      if (done) done.remove();
      parent.insertBefore(card, next);
      bump(1);
    };
  }

  /* The counts are server-rendered, so acting on a card leaves them lying.
     "12 waiting" over eleven cards reads as a bug. */
  function bump(delta) {
    var head = document.querySelector("[data-bincount]");
    if (head) {
      var n = Math.max(0, (parseInt(head.textContent, 10) || 0) + delta);
      head.textContent = n;
    }
    var pip = document.querySelector(".tabbar a[aria-current=page] .pip");
    if (pip) {
      var m = Math.max(0, (parseInt(pip.textContent, 10) || 0) + delta);
      if (m) { pip.textContent = m; } else { pip.remove(); }
    }
  }

  function emptyCheck(stack) {
    if (!stack || stack.querySelector("article.card")) return;
    if (stack.querySelector(".alldone")) return;
    var d = document.createElement("div");
    d.className = "empty alldone";
    d.innerHTML = '<svg class="i" aria-hidden="true"><use href="#i-check"/></svg>' +
      "<b>That is all of them</b><p>Nothing left in this list.</p>";
    stack.appendChild(d);
  }

  function act(form, card, kind) {
    var data = new FormData(form);
    var was = card.getAttribute("data-status");
    var restore = leave(card, kind);
    post("/triage", data).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      toast(LABEL[kind] + ".", "Undo", function () {
        post("/triage", {hunt_id: data.get("hunt_id"),
                         listing_id: data.get("listing_id"),
                         status: was || "wanted"}).then(restore);
      });
    }).catch(function () {
      restore();
      toast("That did not save. Still connected?", null, null, true);
    });
  }

  /* Any form marked data-inplace saves without navigating.
   *
   * Rather than patching the DOM by hand for each one -- a toggle changes the
   * pill, a blocked word changes a count, saving hours changes a sentence --
   * it re-fetches this same page and swaps in the affected panel. The server
   * stays the single source of truth for every label on it, and your scroll
   * position never moves, which is the entire complaint this fixes.
   *
   * data-inplace takes an optional selector; default is the closest panel.
   */
  function refresh(sel) {
    return fetch(location.href, {credentials: "same-origin"})
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        [sel, ".health"].forEach(function (s) {
          var fresh = doc.querySelector(s), cur = document.querySelector(s);
          if (fresh && cur) cur.replaceWith(fresh);
        });
      });
  }

  function inplace(form) {
    /* The target has to be a selector the SERVER also produces. An earlier
       version invented an id on the live element and then asked the freshly
       fetched page for it, which of course did not have one -- so the save
       went through, the toast fired, and the panel kept showing stale state.
       Default to <main>; narrow it with data-inplace="#some-panel-id". */
    var sel = form.getAttribute("data-inplace") || "main";
    var note = form.getAttribute("data-toast");
    var refocus = form.getAttribute("data-refocus");
    fetch(form.getAttribute("action"), {
      method: "POST", credentials: "same-origin",
      headers: {"X-Requested-With": "fetch"},
      body: new URLSearchParams(new FormData(form))
    }).then(function (r) {
      var type = r.headers.get("content-type") || "";
      if (!r.ok) throw new Error(r.status);
      return type.indexOf("json") >= 0 ? r.json() : null;
    }).then(function (body) {
      if (body && body.ok === false) { toast(body.error, null, null, true); return; }
      return refresh(sel).then(function () {
        if (note) toast(note);
        if (refocus) {
          var el = document.querySelector(sel + " [name=" + refocus + "]");
          if (el) { el.value = ""; el.focus(); }
        }
      });
    }).catch(function () {
      toast("That did not save. Still connected?", null, null, true);
    });
  }

  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    var card = form.closest && form.closest("article.card");
    if (!card) {
      if (form.hasAttribute && form.hasAttribute("data-inplace")) {
        ev.preventDefault();
        inplace(form);
      }
      return;
    }

    if (form.getAttribute("action") === "/triage") {
      var kind = form.querySelector('[name=status]').value;
      if (kind !== "saved" && kind !== "dismissed") return;
      ev.preventDefault();
      act(form, card, kind);
      return;
    }
    if (form.classList.contains("addterm")) {
      ev.preventDefault();
      var input = form.querySelector("input[name=term]");
      block(card, input.value);
    }
  });

  /* --- never show me this again ----------------------------------------- */

  var STOP = ("free the and for with new used good great nice condition pick " +
    "pickup curb alert must all set cash only firm obo available need gone " +
    "have has its you your this that from out off not but are was one two " +
    "three size large small item items stuff excellent like very still work " +
    "works working please come first serve served today tomorrow asap sale " +
    "moving take taking away giving give box lot lots plus inch inches").split(" ");

  /* Candidate words off the title, because the useful block is a category --
     "mattress", "recliner", "firewood" -- and it is almost always a noun that
     is already in the title. Typed entry stays for everything else. */
  function suggest(card) {
    var h = card.querySelector(".info h2");
    var seen = {}, out = [];
    (h ? h.textContent : "").toLowerCase().split(/[^a-z0-9]+/).forEach(function (w) {
      if (w.length < 4 || seen[w] || STOP.indexOf(w) >= 0 || /^\d+$/.test(w)) return;
      seen[w] = 1;
      out.push(w);
    });
    return out.slice(0, 6);
  }

  function block(card, term) {
    term = (term || "").trim().toLowerCase();
    if (term.length < 3) {
      toast("Too short to block safely.", null, null, true);
      return;
    }
    var hunt = card.getAttribute("data-hunt");
    var listing = card.getAttribute("data-listing");
    var was = card.getAttribute("data-status");
    post("/settings/exclude", {hunt_id: hunt, term: term})
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok) { toast(res.error, null, null, true); return; }
        var restore = leave(card, "blocked");
        return post("/triage", {hunt_id: hunt, listing_id: listing,
                                status: "dismissed"}).then(function () {
          toast("Never showing “" + term + "” again.", "Undo", function () {
            post("/settings/exclude", {hunt_id: hunt, term: term, remove: "1"});
            post("/triage", {hunt_id: hunt, listing_id: listing,
                             status: was || "free_find"}).then(restore);
          });
        });
      })
      .catch(function () {
        toast("That did not save. Still connected?", null, null, true);
      });
  }

  document.addEventListener("click", function (ev) {
    var t = ev.target.closest && ev.target.closest(".blocktoggle, .chips button");
    if (!t) return;
    var card = t.closest("article.card");
    if (t.classList.contains("blocktoggle")) {
      ev.preventDefault();
      var row = card.querySelector(".blockrow");
      var open = row.hidden;
      row.hidden = !open;
      t.setAttribute("aria-expanded", open ? "true" : "false");
      var chips = row.querySelector(".chips");
      if (open && chips && !chips.childElementCount) {
        suggest(card).forEach(function (w) {
          var b = document.createElement("button");
          b.type = "button";
          b.textContent = w;
          chips.appendChild(b);
        });
        if (!chips.childElementCount) chips.remove();
      }
      return;
    }
    ev.preventDefault();
    block(card, t.textContent);
  });
})();
// Registered after load so it never delays a first paint, and swallowed
// entirely on failure: over plain http (a tailscale address, say) this throws,
// and an install nicety must not put an error in the console of a working page.
if ("serviceWorker" in navigator) {
  addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  });
}
