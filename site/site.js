// The website of the XQ package registry: the search of the list, copy buttons on code.
(() => {
  for (const pre of document.querySelectorAll("pre.code")) {
    const b = document.createElement("button");
    b.className = "copy";
    b.type = "button";
    b.textContent = "Copy";
    b.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText((pre.querySelector("code") ?? pre).innerText);
        b.textContent = "Copied";
      } catch {
        b.textContent = "Select and copy";
      }
      setTimeout(() => (b.textContent = "Copy"), 1500);
    });
    pre.appendChild(b);
  }

  // The search: the packages whose name or description has every word; the words stay in the address.
  const q = document.getElementById("q");
  if (q) {
    const items = [...document.querySelectorAll("#list > li")];
    const empty = document.querySelector(".empty");
    const run = () => {
      const words = q.value.toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      for (const li of items) {
        const yes = words.every((w) => li.dataset.text.includes(w));
        li.hidden = !yes;
        if (yes) shown++;
      }
      if (empty) empty.hidden = shown > 0;
      const url = new URL(location.href);
      if (q.value) url.searchParams.set("q", q.value);
      else url.searchParams.delete("q");
      history.replaceState(null, "", url);
    };
    q.value = new URLSearchParams(location.search).get("q") ?? "";
    q.addEventListener("input", run);
    run();
    if (!q.value) q.focus({ preventScroll: true });
  }
})();
