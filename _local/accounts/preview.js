"use strict";
(() => {
  // FSA Loan positions 1–17. Only the last four values may leave the parser.
  const fields = [
    "Loan Type Code", "Loan Type Description", "Loan Award ID", "Loan Attending School Name",
    "Loan Attending School OPEID", "Loan Date", "Loan Repayment Begin Date", "Loan Period Begin Date",
    "Loan Period End Date", "Loan Amount", "Loan Disbursed Amount", "Loan Canceled Amount",
    "Loan Canceled Date", "Loan Outstanding Principal Balance", "Loan Outstanding Principal Balance as of Date",
    "Loan Outstanding Interest Balance", "Loan Outstanding Interest Balance as of Date",
  ];
  function safeValue(value, isDate) {
    if (!value) return "Not provided";
    if (!isDate) return /^\$(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?$/.test(value)
      ? value : "Unrecognized amount";
    const m = /^(\d{2})\/(\d{2})\/([1-9]\d{3})$/.exec(value);
    if (m) {
      const date = new Date(Date.UTC(+m[3], +m[1] - 1, +m[2]));
      if (date.getUTCMonth() + 1 === +m[1] && date.getUTCDate() === +m[2]) return value;
    }
    return "Unrecognized date";
  }
  function parse(text) {
    const loans = [];
    let last = -1, current = null;
    for (const raw of text.split(/\r\n?|\n/)) {
      const line = raw.trim();
      if (!line) continue;
      const colon = line.indexOf(":");
      if (colon < 1) throw new Error();
      const pos = fields.indexOf(line.slice(0, colon).trim());
      if (pos === 0) {
        if (last === 0 || !line.slice(colon + 1).trim()) throw new Error();
        current = Array(4).fill("Not provided");
        loans.push(current);
        last = 0;
      } else if (pos < 0) {
        // Later loan subrecords, grants, totals, etc. cannot supply a missing date.
        current = null;
      } else {
        if (!current || pos <= last) throw new Error();
        last = pos;
        if (pos >= 13) current[pos - 13] = safeValue(line.slice(colon + 1).trim(), fields[pos].endsWith(" Date"));
      }
    }
    return loans;
  }

  const byId = id => document.getElementById(id);
  const file = byId("file"), select = byId("loan"), result = byId("result"), status = byId("status");
  let loans = [], generation = 0;
  function clear() {
    generation++;
    loans = [];
    file.value = "";
    select.replaceChildren(new Option("Choose a loan", ""));
    byId("picker").hidden = true;
    result.replaceChildren();
    status.textContent = "";
  }
  byId("choose").onclick = () => { clear(); file.click(); };
  file.onchange = async () => {
    const selected = file.files[0];
    clear();
    const run = generation;
    if (!selected) return;
    if (selected.size > 8 * 1024 * 1024) {
      status.textContent = "File exceeds this preview’s 8 MiB limit.";
      return;
    }
    try {
      const text = await selected.text();
      if (run !== generation) return;
      loans = parse(text);
      loans.forEach((_, i) => select.add(new Option(`Loan ${i + 1}`, String(i))));
      byId("picker").hidden = !loans.length;
      status.textContent = loans.length ? "Choose one loan to view its snapshot." : "No supported loan blocks found.";
    } catch {
      status.textContent = "Cannot safely separate these records. Check the text layout for missing boundaries or repeated or out-of-order fields.";
    }
  };
  select.onchange = () => {
    result.replaceChildren();
    if (!select.value) return;
    const [principal, pDate, interest, iDate] = loans[Number(select.value)];
    for (const text of ["Dated snapshot — not a payoff quote.", `Outstanding principal: ${principal}`,
      `Principal as of: ${pDate}`, `Outstanding interest: ${interest}`, `Interest as of: ${iDate}`]) {
      const p = document.createElement("p");
      p.textContent = text;
      result.append(p);
    }
  };
  addEventListener("pagehide", clear);
  addEventListener("pageshow", clear);
})();
