"""WebAuthn / passkeys: unit tests + a real registration/login with a virtual
authenticator (Playwright + Chromium CDP)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from crewhall.web import webauthn

try:
    from playwright.sync_api import sync_playwright
except Exception:  # noqa: BLE001
    sync_playwright = None

CHROMIUM = (shutil.which("chromium") or shutil.which("chromium-browser")
            or shutil.which("google-chrome"))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cbor_uint(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([(major << 5) | n])
    if n < 256:
        return bytes([(major << 5) | 24, n])
    if n < 65536:
        return bytes([(major << 5) | 25]) + struct.pack(">H", n)
    return bytes([(major << 5) | 26]) + struct.pack(">I", n)


def _cbor_bytes(b: bytes) -> bytes:
    return _cbor_uint(2, len(b)) + b


def _cbor_text(s: str) -> bytes:
    raw = s.encode()
    return _cbor_uint(3, len(raw)) + raw


def _cbor_map(items) -> bytes:
    out = _cbor_uint(5, len(items))
    for k, v in items:
        out += (k if isinstance(k, bytes) else _cbor_uint(0, k))
        out += v
    return out


class WebAuthnUnitTest(unittest.TestCase):
    def test_base64url_roundtrip(self):
        raw = os.urandom(40)
        self.assertEqual(webauthn.b64u_decode(webauthn.b64u_encode(raw)), raw)

    def test_cbor_decodes_a_cose_key(self):
        # {1: 2, 3: -7, -1: 1, -2: <x>, -3: <y>}  (EC2 / ES256 / P-256)
        x = bytes(range(32))
        y = bytes(range(32, 64))
        cose = _cbor_map([
            (_cbor_uint(0, 1), _cbor_uint(0, 2)),
            (_cbor_uint(0, 3), _cbor_uint(1, 6)),   # -7
            (_cbor_uint(1, 0), _cbor_uint(0, 1)),   # -1 = 1 (P-256)
            (_cbor_uint(1, 1), _cbor_bytes(x)),     # -2
            (_cbor_uint(1, 2), _cbor_bytes(y)),     # -3
        ])
        key = webauthn.parse_cose_key(cose)
        self.assertEqual((key["kty"], key["alg"], key["crv"]), (2, -7, 1))
        self.assertEqual(key["x"], x)
        self.assertEqual(key["y"], y)

    def test_der_signature_parsing(self):
        # SEQUENCE { INTEGER r, INTEGER s }
        r, s = 0x0102, 0x03FF
        body = b"\x02\x02\x01\x02" + b"\x02\x02\x03\xff"
        der = b"\x30" + bytes([len(body)]) + body
        self.assertEqual(webauthn._der_signature(der), (r, s))

    def test_parse_auth_data_with_credential(self):
        rp_hash = hashlib.sha256(b"localhost").digest()
        cred_id = b"credential-id-1234"
        cose = _cbor_map([(_cbor_uint(0, 1), _cbor_uint(0, 2))])
        flags = 0x41  # UP + AT
        auth = rp_hash + bytes([flags]) + struct.pack(">I", 7) + b"\x00" * 16 \
            + struct.pack(">H", len(cred_id)) + cred_id + cose
        parsed = webauthn.parse_auth_data(auth)
        self.assertTrue(parsed["up"])
        self.assertEqual(parsed["credential_id"], cred_id)
        self.assertEqual(parsed["sign_count"], 7)
        self.assertTrue(parsed["cose_bytes"].startswith(b"\xa1"))

    def test_rejects_bad_client_data(self):
        challenge = os.urandom(32)
        data = base64.urlsafe_b64encode(json.dumps({
            "type": "webauthn.get", "challenge": webauthn.b64u_encode(challenge),
            "origin": "https://evil.example"}).encode()).rstrip(b"=").decode()
        with self.assertRaises(ValueError):
            webauthn.verify_client_data(data, "webauthn.get", challenge, "http://localhost")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


@unittest.skipUnless(sync_playwright and CHROMIUM, "needs playwright + chromium")
class WebAuthnEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ati-webauthn-", dir="/tmp")
        cls.run_dir = os.path.join(cls.root, "run")
        os.makedirs(cls.run_dir, mode=0o700)
        cls.sock = os.path.join(cls.root, "d.sock")
        cls.port = _free_port()
        cls.env = {
            **os.environ,
            "XDG_CONFIG_HOME": os.path.join(cls.root, "config"),
            "XDG_STATE_HOME": os.path.join(cls.root, "state"),
            "XDG_RUNTIME_DIR": cls.run_dir,
            "CREWHALL_TMUX_SOCKET": f"at_wa_{os.getpid()}",
            "CREWHALL_HOOKS": "0", "CREWHALL_CONVERSATIONS": "0",
            "PYTHONPATH": REPO,
        }
        from crewhall.web import auth

        cls._saved = {k: os.environ.get(k) for k in
                      ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")}
        os.environ.update({k: cls.env[k] for k in cls._saved})
        cls.master = auth.generate_token(rotate=True)
        cls.daemon = subprocess.Popen(
            [sys.executable, "-P", "-m", "crewhall.daemon", "--foreground", "--socket", cls.sock],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert _wait(lambda: os.path.exists(cls.sock)), "daemon did not start"
        cls.web = subprocess.Popen(
            [sys.executable, "-P", "-m", "crewhall", "web", "--port", str(cls.port),
             "--socket", cls.sock, "--require-auth"],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert _wait(lambda: _port_open(cls.port)), "web did not start"

    @classmethod
    def tearDownClass(cls):
        for proc in (cls.web, cls.daemon):
            try:
                proc.terminate(); proc.wait(timeout=10)
            except Exception:
                try: proc.kill()
                except Exception: pass
        subprocess.run(["tmux", "-L", cls.env["CREWHALL_TMUX_SOCKET"], "kill-server"],
                       capture_output=True)
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_register_then_login_with_a_passkey(self):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/login", json.dumps({"token": self.master}),
                     {"Content-Type": "application/json", "Origin": f"http://localhost:{self.port}"})
        resp = conn.getresponse(); resp.read()
        cookie = resp.getheader("Set-Cookie").split(";", 1)[0]
        conn.close()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            try:
                context = browser.new_context(viewport={"width": 1000, "height": 800})
                context.add_cookies([{"name": "at_session", "value": cookie.split("=", 1)[1],
                                      "url": f"http://localhost:{self.port}"}])
                page = context.new_page()
                page.goto(f"http://localhost:{self.port}/")
                cdp = context.new_cdp_session(page)
                cdp.send("WebAuthn.enable")
                cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
                    "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
                    "hasUserVerification": True, "isUserVerified": True,
                    "automaticPresenceSimulation": True}})
                result = page.evaluate("""async () => {
                  const b64u = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
                  const unb64u = s => { s=s.replace(/-/g,'+').replace(/_/g,'/'); const bin=atob(s); const u=new Uint8Array(bin.length); for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i); return u; };
                  const begin = await (await fetch('/api/webauthn/register/begin',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
                  const pk = begin.publicKey;
                  const cred = await navigator.credentials.create({publicKey:{
                    challenge:unb64u(pk.challenge), rp:pk.rp,
                    user:{id:unb64u(pk.user.id),name:pk.user.name,displayName:pk.user.displayName},
                    pubKeyCredParams:pk.pubKeyCredParams, timeout:pk.timeout, attestation:pk.attestation,
                    authenticatorSelection:pk.authenticatorSelection,
                    excludeCredentials:(pk.excludeCredentials||[]).map(c=>({type:'public-key',id:unb64u(c.id)}))}});
                  const fin = await (await fetch('/api/webauthn/register/finish',{method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({ceremony:begin.ceremony,id:b64u(cred.rawId),
                      clientDataJSON:b64u(cred.response.clientDataJSON),
                      attestationObject:b64u(cred.response.attestationObject),label:'test'})})).json();
                  return fin;
                }""")
                self.assertTrue(result.get("ok"), result)

                # Login with the passkey (no cookie needed).
                login = page.evaluate("""async () => {
                  const b64u = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
                  const unb64u = s => { s=s.replace(/-/g,'+').replace(/_/g,'/'); const bin=atob(s); const u=new Uint8Array(bin.length); for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i); return u; };
                  const begin = await (await fetch('/api/webauthn/login/begin',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
                  const pk = begin.publicKey;
                  const cred = await navigator.credentials.get({publicKey:{
                    challenge:unb64u(pk.challenge), rpId:pk.rpId, timeout:pk.timeout,
                    userVerification:pk.userVerification, allowCredentials:[]}});
                  const r = await fetch('/api/webauthn/login/finish',{method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({ceremony:begin.ceremony,id:b64u(cred.rawId),
                      clientDataJSON:b64u(cred.response.clientDataJSON),
                      authenticatorData:b64u(cred.response.authenticatorData),
                      signature:b64u(cred.response.signature)})});
                  return {status:r.status, ok:(await r.json().catch(()=>({}))).ok};
                }""")
                self.assertEqual(login.get("status"), 200, login)
                self.assertTrue(login.get("ok"))
            finally:
                browser.close()

    def test_login_page_offers_a_passkey_button(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            try:
                page = browser.new_page()
                page.goto(f"http://localhost:{self.port}/login")
                page.wait_for_selector("#pk:not(.init-hidden)", timeout=5000)
            finally:
                browser.close()

    def test_login_without_registered_passkey_is_rejected(self):
        import http.client

        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            try:
                page = browser.new_page()
                page.goto(f"http://localhost:{self.port}/login")
                out = page.evaluate("""async () => {
                  const r = await fetch('/api/webauthn/login/finish',{method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({ceremony:'nope',id:'AAAA',clientDataJSON:'AAAA',
                      authenticatorData:'AAAA',signature:'AAAA'})});
                  return r.status;
                }""")
                self.assertEqual(out, 400)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
