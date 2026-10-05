"use strict";
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await fetch("/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: document.getElementById("t").value }),
  });
  if (r.ok) { location.href = "/"; }
  else { document.getElementById("e").textContent = "invalid token"; }
});
