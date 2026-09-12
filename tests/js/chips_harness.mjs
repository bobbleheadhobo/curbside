// Exercise the pill editor's logic in isolation, with a minimal DOM.
import fs from "node:fs";
const src = fs.readFileSync(process.argv[2], "utf8");
const start = src.indexOf("/* Search terms as pills.");
const code = src.slice(start);

// --- tiny DOM ---------------------------------------------------------------
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {};
    this.listeners = {}; this.value = ""; this.hidden = false; this.className = "";
    this._text = "";
  }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  insertBefore(n) { this.children.push(n); n.parentNode = this; return n; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(k, fn) { (this.listeners[k] ||= []).push(fn); }
  fire(k, ev = {}) { (this.listeners[k] || []).forEach(f => f(ev)); }
  querySelector() { return null; }
  focus() {}
  get childElementCount() { return this.children.length; }
  set textContent(v) { this._text = v; this.children = []; }
  get textContent() {
    return this.children.length
      ? this.children.map(c => c.textContent).join("") : this._text;
  }
}
class TextNode { constructor(t) { this._t = t; } get textContent() { return this._t; } }

const ta = new El("textarea");
ta.value = "tv stand\nmedia console";
const form = new El("form");
const parent = new El("div");
parent.appendChild(ta);
ta.form = form;

global.document = {
  getElementById: id => (id === "f-queries" ? ta : null),
  querySelector: () => null,
  createElement: t => { var e = new El(t); e.form = form; return e; },
  createTextNode: t => new TextNode(t),
  addEventListener: () => {},
};
global.addEventListener = () => {};
global.window = { matchMedia: () => ({ matches: false }) };

eval(code);

const wrap = parent.children.find(c => c.className === "termedit");
const pills = wrap.children[0], input = wrap.children[1];
const names = () => pills.children.map(p => p.children[0].textContent);

const out = [];
const eq = (label, got, want) => out.push(
  `${JSON.stringify(got) === JSON.stringify(want) ? "ok" : "FAIL"} ${label} ` +
  `got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);

eq("seeds pills from the textarea", names(), ["tv stand", "media console"]);
eq("hides the textarea", ta.hidden, true);

let submitted = false;
input.value = "credenza";
input.fire("keydown", { key: "Enter", preventDefault() { submitted = true; } });
eq("Enter adds a term", names(), ["tv stand", "media console", "credenza"]);
eq("Enter does not submit the form", submitted, true);   // preventDefault ran
eq("syncs the textarea", ta.value, "tv stand\nmedia console\ncredenza");

input.value = "TV STAND";                       // duplicate, different case
input.fire("keydown", { key: "Enter", preventDefault() {} });
eq("ignores a case-insensitive duplicate", names(),
   ["tv stand", "media console", "credenza"]);

input.value = "";
input.fire("keydown", { key: "Backspace", preventDefault() {} });
eq("backspace pulls the last term back to edit", names(),
   ["tv stand", "media console"]);
eq("... and puts it in the box", input.value, "credenza");

pills.children[0].children[1].fire("click");    // the x on the first pill
eq("the x removes one pill", names(), ["media console"]);
eq("... and syncs the textarea", ta.value, "media console");

input.value = "left in the box";
form.fire("submit");
eq("a term left in the box is not lost on submit", names(),
   ["media console", "left in the box"]);

console.log(out.join("\n"));
if (out.some(l => l.startsWith("FAIL"))) process.exit(1);
