// Runs named functions out of web/index.html in node.
//
// The page is one file with no build step and no module boundary, which is a
// deliberate simplicity and makes it awkward to test. Rather than duplicate the
// logic here -- a copy that cannot catch a change to the original -- this lifts
// the real function sources out of the file by name and evaluates them. The
// page's own definitions are what run.
//
// Usage: node web_harness.js <index.html> <<< '{"state": {...}, "calls": [...]}'

const fs = require("fs");

const file = process.argv[2];
const src = fs.readFileSync(file, "utf8");
const input = JSON.parse(fs.readFileSync(0, "utf8"));

// Balanced-brace extraction: regexes cannot find the end of a function body
// that contains template literals full of braces, and this page is mostly that.
function fnSource(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`web/index.html has no function ${name}()`);
  let depth = 0, i = src.indexOf("{", start);
  for (;; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) break;
  }
  return src.slice(start, i + 1);
}

// The small formatting helpers are consts, not functions.
const helpers = src.split("\n").filter((l) =>
  /^const (esc|n0|n1|W_TREND|W_DOT|W_BOX|TREND_TAU_DAYS) = /.test(l)
  || l.startsWith('  (c) => ({ "&"')).join("\n");

const WANTED = ["uncountedKind", "uncountedReason", "uncountedSummary",
                "passiveAverage", "outFor", "partialNote", "statsFor", "stat",
                "avgRow", "bar", "statsBlock", "dayListCard", "renderRollup",
                "weightSeries", "weightFit", "smoothWeight", "weightRate", "weightGeometry",
                "chartWidth", "wireWeightChart",
                "weightChart", "shortDate"];

globalThis.state = input.state;
// Enough of an element for the render functions to write into. querySelector
// returns null so the hover wiring bows out the way it does for a chart that
// was not drawn -- node has no layout, so there is nothing to attach to.
globalThis.el = () => ({ innerHTML: "", querySelector: () => null });
eval(helpers + "\n" + WANTED.map(fnSource).join("\n"));

const api = Object.fromEntries(WANTED.map((n) => [n, eval(n)]));
const out = {};
for (const [label, call] of Object.entries(input.calls)) {
  const [name, ...args] = call;
  out[label] = api[name](...args);
}
process.stdout.write(JSON.stringify(out));
