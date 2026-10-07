"use strict";
const $ = id => document.getElementById(id);
const b64u = buf => btoa(String.fromCharCode(...new Uint8Array(buf)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const unb64u = s => {
  s = String(s).replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(s), u = new Uint8Array(bin.length);
  for(let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
  return u;
};

$("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await fetch("/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: $("t").value }),
  });
  if (r.ok) { location.href = "/"; }
  else { $("e").textContent = "invalid token"; }
});

async function passkeyLogin(){
  $("e").textContent = "";
  try{
    const begin = await (await fetch("/api/webauthn/login/begin", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).json();
    if(!begin.ok) throw new Error(begin.error || "cannot start");
    const pk = begin.publicKey;
    const cred = await navigator.credentials.get({ publicKey: {
      challenge: unb64u(pk.challenge), rpId: pk.rpId, timeout: pk.timeout,
      userVerification: pk.userVerification, allowCredentials: [],
    }});
    const body = {
      ceremony: begin.ceremony, id: b64u(cred.rawId),
      clientDataJSON: b64u(cred.response.clientDataJSON),
      authenticatorData: b64u(cred.response.authenticatorData),
      signature: b64u(cred.response.signature),
      userHandle: cred.response.userHandle ? b64u(cred.response.userHandle) : null,
    };
    const fin = await fetch("/api/webauthn/login/finish", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if(fin.ok){ location.href = "/"; }
    else { $("e").textContent = "passkey rejected"; }
  }catch(e){ $("e").textContent = e.message || "passkey failed"; }
}

if(window.PublicKeyCredential){
  const btn = $("pk");
  btn.classList.remove("init-hidden");
  btn.addEventListener("click", passkeyLogin);
}
