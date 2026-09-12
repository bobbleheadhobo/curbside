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
    swipeAway(el, close);
    announce(text);
    return close;
  }

  /* Swipe the toast away.
   *
   * It sits at the bottom of the screen over the list, and after a run of
   * triage it is the thing in your way -- seven seconds is a long time to wait
   * for a bar you have already read.
   *
   * Sideways or downwards, the two directions that mean "off" given where it
   * lives. Upward resists, because there is nothing up there. It only ever
   * DISMISSES: the save or the dismissal stays applied, exactly as when the
   * timer runs out. Undo is the button, and a swipe past it must not press it.
   */
  var TOAST_THRESHOLD = 64;

  function swipeAway(el, close) {
    var t = null;

    el.addEventListener("touchstart", function (ev) {
      if (ev.touches.length !== 1) return;
      t = {x: ev.touches[0].clientX, y: ev.touches[0].clientY,
           axis: null, dx: 0, dy: 0, moved: false};
      el.classList.add("dragging");
    }, {passive: true});

    el.addEventListener("touchmove", function (ev) {
      if (!t) return;
      var dx = ev.touches[0].clientX - t.x;
      var dy = ev.touches[0].clientY - t.y;
      if (!t.axis) {
        if (Math.abs(dx) < SLOP && Math.abs(dy) < SLOP) return;
        t.axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
      }
      // Nothing lives above the toast, so an upward drag drags heavily and
      // never reaches the threshold.
      if (t.axis === "y" && dy < 0) dy /= 4;
      t.moved = true;
      t.dx = t.axis === "x" ? dx : 0;
      t.dy = t.axis === "y" ? dy : 0;
      el.style.setProperty("--tx", t.dx + "px");
      el.style.setProperty("--ty", t.dy + "px");
      el.style.opacity = Math.max(
        0.3, 1 - Math.max(Math.abs(t.dx), Math.abs(t.dy)) / 200).toFixed(2);
      if (ev.cancelable) ev.preventDefault();
    }, {passive: false});

    function end() {
      if (!t) return;
      var gone = Math.abs(t.dx) >= TOAST_THRESHOLD || t.dy >= TOAST_THRESHOLD;
      var moved = t.moved;
      t = null;
      el.classList.remove("dragging");
      if (moved) swallowNextClick(el);
      if (gone) {
        // The drag set an inline opacity, which outranks the class, so the
        // toast would slide out still half-visible and then vanish.
        el.style.opacity = "0";
        // Keep the offset: `close` drops the `up` class and the toast carries
        // on out from where the finger left it rather than jumping back first.
        close();
        return;
      }
      el.style.removeProperty("--tx");
      el.style.removeProperty("--ty");
      el.style.opacity = "";
    }

    el.addEventListener("touchend", end);
    el.addEventListener("touchcancel", end);
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

  /* --- swipe a card to save or dismiss ---------------------------------- */
  /*
   * Right saves, left dismisses -- the same directions as the buttons sit in,
   * and the same colours the verdict badge uses, so the gesture is the buttons
   * rather than a second vocabulary to learn.
   *
   * It is an ADDITION. Both buttons stay exactly where they were: a gesture is
   * invisible, undiscoverable and unavailable to anyone not using a touchscreen,
   * so nothing may be reachable only this way.
   *
   * `/saved` offers only Dismiss, and the hunt views offer neither, so the
   * available directions are read from the buttons actually on the card. A
   * swipe towards an action that is not there springs back.
   */
  var THRESHOLD = 72;              // px of travel before it commits
  var SLOP = 12;                   // px before we decide the axis at all

  function triageForms(card) {
    var out = {};
    var inputs = card.querySelectorAll('.actions form [name=status]');
    Array.prototype.forEach.call(inputs, function (el) {
      if (el.value === "saved" || el.value === "dismissed") {
        out[el.value] = el.parentNode;
      }
    });
    return out;
  }

  var sw = null;

  function hint(card, kind) {
    var el = card.querySelector(".swipehint");
    if (!el) {
      el = document.createElement("div");
      el.className = "swipehint";
      card.insertBefore(el, card.firstChild);
    }
    el.className = "swipehint " + kind;
    el.innerHTML = '<svg class="i" aria-hidden="true"><use href="#' +
      ICON[kind] + '"/></svg>' + LABEL[kind];
    return el;
  }

  function slide(card, x) {
    card.style.setProperty("--sx", x + "px");
  }

  function endSwipe(commit) {
    if (!sw) return;
    var card = sw.card, kind = sw.kind, form = kind ? sw.forms[kind] : null;
    var moved = sw.moved;
    sw = null;

    if (moved) swallowNextClick(card);

    if (commit && form) {
      // Carry on out the way it was already going. Yanking the card home and
      // THEN folding it from the middle was the whole of the jank.
      //
      // The offset moves from the inline variable to a CLASS here, and that is
      // load-bearing: undo restores a card by removing `going` and the status
      // class, so a class-based transform disappears with them. An inline one
      // would not, and an undone card would come back off-screen.
      slide(card, 0);
      card.classList.remove("releasing");
      card.classList.add("flinging");
      act(form, card, kind);
      setTimeout(function () {
        card.classList.remove("swiping", "flinging");
        var el = card.querySelector(".swipehint");
        if (el) el.remove();
      }, reduced ? 0 : 620);
      return;
    }

    // Not committed: spring home, THEN drop the classes -- the transform only
    // applies while `swiping` is on, so clearing it first would snap.
    card.classList.add("releasing");
    slide(card, 0);
    setTimeout(function () {
      card.classList.remove("swiping", "releasing");
      card.style.removeProperty("--sx");
      var el = card.querySelector(".swipehint");
      if (el) el.remove();
    }, reduced ? 0 : 340);
  }

  /* Eat the click a touch ends in, once.
   *
   * A drag usually suppresses the click by itself, so a listener that waits
   * for one would sit there and eat the NEXT real tap on that element instead
   * -- a swipe that went nowhere, then a tap on the title that does nothing.
   * It expires.
   */
  function swallowNextClick(el) {
    function done() {
      el.removeEventListener("click", swallow, true);
      clearTimeout(timer);
    }
    function swallow(ev) {
      ev.preventDefault();
      ev.stopPropagation();
      done();
    }
    el.addEventListener("click", swallow, true);
    var timer = setTimeout(done, 700);
  }

  document.addEventListener("touchstart", function (ev) {
    if (sw || ev.touches.length !== 1) return;
    var t = ev.target;
    var card = t.closest && t.closest("article.card");
    if (!card || card.classList.contains("going")) return;
    var forms = triageForms(card);
    if (!forms.saved && !forms.dismissed) return;      // nothing to swipe to
    sw = {card: card, forms: forms, x: ev.touches[0].clientX,
          y: ev.touches[0].clientY, axis: null, moved: false, kind: null};
  }, {passive: true});

  document.addEventListener("touchmove", function (ev) {
    if (!sw) return;
    var dx = ev.touches[0].clientX - sw.x;
    var dy = ev.touches[0].clientY - sw.y;

    if (!sw.axis) {
      if (Math.abs(dy) > SLOP && Math.abs(dy) > Math.abs(dx)) {
        sw = null;                       // a scroll, not a swipe: let it go
        return;
      }
      if (Math.abs(dx) < SLOP) return;
      sw.axis = "x";
      sw.card.classList.add("swiping");
    }

    var kind = dx > 0 ? "saved" : "dismissed";
    if (!sw.forms[kind]) {
      // Nothing in that direction. Let it move a little so it feels alive,
      // but never far enough to look like it will commit.
      slide(sw.card, Math.max(-24, Math.min(24, dx / 3)));
      sw.kind = null;
      // Still movement, even though nothing will come of it: without this the
      // click is not swallowed, and a dead-direction drag released over the
      // title opens the marketplace.
      sw.moved = true;
      if (ev.cancelable) ev.preventDefault();
      return;
    }
    sw.kind = kind;
    sw.moved = true;
    hint(sw.card, kind).style.opacity =
      Math.min(1, Math.abs(dx) / THRESHOLD).toFixed(2);
    slide(sw.card, dx);
    if (ev.cancelable) ev.preventDefault();
  }, {passive: false});

  document.addEventListener("touchend", function () {
    if (!sw) return;
    var dx = parseFloat(sw.card.style.getPropertyValue("--sx")) || 0;
    endSwipe(!!sw.kind && Math.abs(dx) >= THRESHOLD);
  });

  document.addEventListener("touchcancel", function () { endSwipe(false); });
})();
// Registered after load so it never delays a first paint, and swallowed
// entirely on failure: over plain http (a tailscale address, say) this throws,
// and an install nicety must not put an error in the console of a working page.
if ("serviceWorker" in navigator) {
  addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  });
}

/* Search terms as pills.
 *
 * Each term is a whole separate search, and a textarea of lines does not say
 * that -- "tv stand media console" typed on one line is one bad search that
 * finds nothing, and looks identical to two good ones.
 *
 * The <textarea> stays the field that posts, hidden, and is rewritten from the
 * pills on every change. With this file missing you get the textarea, one term
 * per line, and everything still works -- including "Suggest terms", which is a
 * real submit button and needs no script at all.
 */
(function () {
  var ta = document.getElementById("f-queries");
  if (!ta) return;

  var label = document.querySelector('label[for="f-queries"]');
  var wrap = document.createElement("div");
  wrap.className = "termedit";
  var pills = document.createElement("div");
  pills.className = "terms";
  var input = document.createElement("input");
  input.type = "text";
  input.id = "f-queries-add";
  input.autocapitalize = "none";
  input.setAttribute("enterkeyhint", "enter");
  input.placeholder = "tv stand";
  wrap.appendChild(pills);
  wrap.appendChild(input);
  ta.parentNode.insertBefore(wrap, ta.nextSibling);
  ta.hidden = true;
  // The label pointed at the textarea, which is now hidden: move it to the
  // control that actually takes the typing, or tapping the label does nothing.
  if (label) label.setAttribute("for", input.id);

  function terms() {
    return ta.value.split("\n").map(function (t) { return t.trim(); })
             .filter(function (t) { return t.length; });
  }

  function write(list) {
    ta.value = list.join("\n");
    render();
  }

  function render() {
    pills.textContent = "";
    terms().forEach(function (term, i) {
      var pill = document.createElement("span");
      pill.className = "term";
      pill.appendChild(document.createTextNode(term));
      var x = document.createElement("button");
      x.type = "button";                     // never submits the form
      x.setAttribute("aria-label", "Remove " + term);
      x.textContent = "×";
      x.addEventListener("click", function () {
        var list = terms();
        list.splice(i, 1);
        write(list);
        input.focus();
      });
      pill.appendChild(x);
      pills.appendChild(pill);
    });
    pills.hidden = !pills.childElementCount;
  }

  function commit() {
    var term = input.value.trim();
    if (!term) return;
    var list = terms();
    // Case-insensitive, like the server's own cleanup: two spellings of one
    // search are two identical requests per tick, forever.
    var dupe = list.some(function (t) {
      return t.toLowerCase() === term.toLowerCase();
    });
    if (!dupe) list.push(term);
    input.value = "";
    write(list);
  }

  input.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" || ev.key === ",") {
      // Enter here means "that is one term", NOT "submit the form" -- which is
      // what it would otherwise do, saving a half-filled want.
      ev.preventDefault();
      commit();
      return;
    }
    if (ev.key === "Backspace" && !input.value && terms().length) {
      ev.preventDefault();
      var list = terms();
      input.value = list.pop();             // back into the box to edit, not gone
      write(list);
    }
  });

  // A term typed and left sitting in the box is one you meant to add.
  input.addEventListener("blur", commit);
  // Guarded: a null form here would throw and take the whole editor with it,
  // leaving a hidden textarea and no way to type at all.
  var form = input.form || ta.form;
  if (form) form.addEventListener("submit", commit);

  render();
})();
