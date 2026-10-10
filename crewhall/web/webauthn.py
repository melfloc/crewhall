"""WebAuthn / passkeys with the standard library only.

No third-party dependencies: a minimal CBOR decoder, COSE key parsing and the
two signature algorithms passkeys actually use (ES256 and RS256) are implemented
here. Attestation statements are not verified (the RP requests ``none``): we
care about the *assertion* (login), which is always verified against the stored
public key.

A passkey is only usable in a **secure context** (``https://`` or ``localhost``).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import struct
import threading
import time
from typing import Any

from .. import brand

# -- base64url ---------------------------------------------------------------
def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


# -- minimal CBOR decoder ----------------------------------------------------
def _decode(data: bytes, i: int) -> tuple[Any, int]:
    if i >= len(data):
        raise ValueError("cbor: truncated")
    ib = data[i]
    major, ai = ib >> 5, ib & 0x1F
    i += 1
    if ai < 24:
        n = ai
    elif ai == 24:
        n = data[i]
        i += 1
    elif ai == 25:
        n = struct.unpack(">H", data[i:i + 2])[0]
        i += 2
    elif ai == 26:
        n = struct.unpack(">I", data[i:i + 4])[0]
        i += 4
    elif ai == 27:
        n = struct.unpack(">Q", data[i:i + 8])[0]
        i += 8
    else:
        raise ValueError("cbor: unsupported additional info")
    if major == 0:
        return n, i
    if major == 1:
        return -1 - n, i
    if major == 2:
        return bytes(data[i:i + n]), i + n
    if major == 3:
        return data[i:i + n].decode("utf-8"), i + n
    if major == 4:
        out = []
        for _ in range(n):
            value, i = _decode(data, i)
            out.append(value)
        return out, i
    if major == 5:
        out_map: dict[Any, Any] = {}
        for _ in range(n):
            key, i = _decode(data, i)
            value, i = _decode(data, i)
            out_map[key] = value
        return out_map, i
    if major == 6:  # a tag: the tagged value is what matters
        return _decode(data, i)
    if major == 7:
        if ai == 20:
            return False, i
        if ai == 21:
            return True, i
        if ai in (22, 23):
            return None, i
    raise ValueError("cbor: unsupported value")


def cbor_decode(data: bytes) -> Any:
    value, _ = _decode(data, 0)
    return value


# -- COSE keys ---------------------------------------------------------------
def parse_cose_key(cose: bytes) -> dict[str, Any]:
    m = cbor_decode(cose)
    if not isinstance(m, dict):
        raise ValueError("COSE key is not a map")
    kty, alg = m.get(1), m.get(3)
    if kty == 2:  # EC2
        return {"kty": 2, "alg": alg, "crv": m.get(-1), "x": m.get(-2), "y": m.get(-3)}
    if kty == 3:  # RSA
        return {"kty": 3, "alg": alg, "n": m.get(-1), "e": m.get(-2)}
    raise ValueError(f"unsupported COSE key type {kty!r}")


# -- ECDSA P-256 (ES256) -----------------------------------------------------
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
      0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _ec_add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    x1, y1 = p
    x2, y2 = q
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p == q:
        lam = (3 * x1 * x1 + _A) * pow(2 * y1, -1, _P) % _P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _ec_mul(k: int, point):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _ec_add(result, addend)
        addend = _ec_add(addend, addend)
        k >>= 1
    return result


def _der_signature(der: bytes) -> tuple[int, int]:
    if len(der) < 8 or der[0] != 0x30:
        raise ValueError("bad DER signature")
    i = 2
    if der[1] & 0x80:
        i = 2 + (der[1] & 0x7F)
    if der[i] != 0x02:
        raise ValueError("bad DER signature")
    rlen = der[i + 1]
    r = int.from_bytes(der[i + 2:i + 2 + rlen], "big")
    i += 2 + rlen
    if der[i] != 0x02:
        raise ValueError("bad DER signature")
    slen = der[i + 1]
    s = int.from_bytes(der[i + 2:i + 2 + slen], "big")
    return r, s


def _es256_verify(key: dict[str, Any], message: bytes, signature: bytes) -> bool:
    x, y = key.get("x"), key.get("y")
    if not isinstance(x, bytes) or not isinstance(y, bytes):
        return False
    try:
        r, s = _der_signature(signature)
    except ValueError:
        return False
    if not (1 <= r < _N and 1 <= s < _N):
        return False
    z = int.from_bytes(hashlib.sha256(message).digest(), "big")
    w = pow(s, -1, _N)
    point = _ec_add(_ec_mul((z * w) % _N, _G),
                    _ec_mul((r * w) % _N, (int.from_bytes(x, "big"), int.from_bytes(y, "big"))))
    return point is not None and point[0] % _N == r


# -- RSA PKCS#1 v1.5 (RS256) -------------------------------------------------
_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _rs256_verify(key: dict[str, Any], message: bytes, signature: bytes) -> bool:
    n, e = key.get("n"), key.get("e")
    if not isinstance(n, bytes) or not isinstance(e, bytes):
        return False
    n_int = int.from_bytes(n, "big")
    k = (n_int.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n_int:
        return False
    em = pow(s, int.from_bytes(e, "big"), n_int).to_bytes(k, "big")
    digest = hashlib.sha256(message).digest()
    padding = k - len(_SHA256_DIGESTINFO) - len(digest) - 3
    if padding < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding + b"\x00" + _SHA256_DIGESTINFO + digest
    return hmac.compare_digest(em, expected)


def verify_signature(key: dict[str, Any], message: bytes, signature: bytes) -> bool:
    alg = key.get("alg")
    if key.get("kty") == 2 and alg in (-7, None):
        return _es256_verify(key, message, signature)
    if key.get("kty") == 3 and alg in (-257, None):
        return _rs256_verify(key, message, signature)
    return False


# -- authenticator data ------------------------------------------------------
def parse_auth_data(auth_data: bytes) -> dict[str, Any]:
    if len(auth_data) < 37:
        raise ValueError("authenticator data too short")
    flags = auth_data[32]
    out: dict[str, Any] = {
        "rp_id_hash": auth_data[:32], "flags": flags,
        "sign_count": int.from_bytes(auth_data[33:37], "big"),
        "up": bool(flags & 0x01), "uv": bool(flags & 0x04), "at": bool(flags & 0x40),
    }
    if flags & 0x40:  # attested credential data present
        rest = auth_data[37:]
        if len(rest) < 18:
            raise ValueError("attested credential data too short")
        clen = int.from_bytes(rest[16:18], "big")
        out["aaguid"] = rest[:16]
        out["credential_id"] = rest[18:18 + clen]
        cose, end = _decode(rest, 18 + clen)
        out["cose_key"] = cose
        out["cose_bytes"] = rest[18 + clen:end]
    return out


def _expected_origin(headers: Any) -> str:
    origin = headers.get("Origin", "")
    return origin.rstrip("/")


def rp_id_from_host(host: str) -> str:
    host = (host or "").strip()
    if host.startswith("["):  # IPv6
        return host[1:host.find("]")]
    return host.rsplit(":", 1)[0] if ":" in host else host


# -- ceremonies --------------------------------------------------------------
def new_challenge() -> bytes:
    return secrets.token_bytes(32)


def verify_client_data(client_data_b64: str, expected_type: str, challenge: bytes,
                       expected_origin: str) -> dict[str, Any]:
    raw = b64u_decode(client_data_b64)
    data = json.loads(raw.decode("utf-8"))
    if data.get("type") != expected_type:
        raise ValueError("unexpected clientData type")
    if not hmac.compare_digest(b64u_decode(str(data.get("challenge", ""))), challenge):
        raise ValueError("challenge mismatch")
    origin = str(data.get("origin", "")).rstrip("/")
    if origin != expected_origin:
        raise ValueError("origin mismatch")
    return data


def verify_registration(client_data_b64: str, attestation_b64: str, challenge: bytes,
                        expected_origin: str, rp_id: str) -> dict[str, Any]:
    """Return {credential_id, cose_bytes, alg, sign_count} or raise ValueError."""
    verify_client_data(client_data_b64, "webauthn.create", challenge, expected_origin)
    att = cbor_decode(b64u_decode(attestation_b64))
    if not isinstance(att, dict) or "authData" not in att:
        raise ValueError("bad attestation object")
    auth = parse_auth_data(att["authData"])
    if not auth["up"]:
        raise ValueError("user presence flag not set")
    if not hmac.compare_digest(auth["rp_id_hash"], hashlib.sha256(rp_id.encode()).digest()):
        raise ValueError("rpId hash mismatch")
    if not auth.get("credential_id") or not auth.get("cose_bytes"):
        raise ValueError("no credential in attestation")
    cose = auth["cose_key"]
    return {
        "credential_id": auth["credential_id"],
        "cose_bytes": auth["cose_bytes"],
        "alg": cose.get(3),
        "sign_count": auth["sign_count"],
    }


def verify_assertion(client_data_b64: str, auth_data_b64: str, signature_b64: str,
                     challenge: bytes, expected_origin: str, rp_id: str,
                     public_key: dict[str, Any], stored_count: int) -> int:
    """Verify an assertion; return the new sign count, or raise ValueError."""
    verify_client_data(client_data_b64, "webauthn.get", challenge, expected_origin)
    auth_data = b64u_decode(auth_data_b64)
    auth = parse_auth_data(auth_data)
    if not auth["up"]:
        raise ValueError("user presence flag not set")
    if not hmac.compare_digest(auth["rp_id_hash"], hashlib.sha256(rp_id.encode()).digest()):
        raise ValueError("rpId hash mismatch")
    client_hash = hashlib.sha256(b64u_decode(client_data_b64)).digest()
    message = auth_data + client_hash
    if not verify_signature(public_key, message, b64u_decode(signature_b64)):
        raise ValueError("signature verification failed")
    new_count = auth["sign_count"]
    if stored_count and new_count and new_count <= stored_count:
        raise ValueError("sign count did not increase (possible cloned authenticator)")
    return new_count


def otpauth_label(rp_id: str) -> str:
    return f"crewhall ({rp_id})"


# -- credential store --------------------------------------------------------
_LOCK = threading.RLock()


def _config_dir() -> str:
    path = brand.config_dir()
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def store_path() -> str:
    return os.path.join(_config_dir(), "web-credentials.json")


def _load() -> list[dict[str, Any]]:
    try:
        with open(store_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    records = data.get("credentials") if isinstance(data, dict) else None
    return records if isinstance(records, list) else []


def _save(records: list[dict[str, Any]]) -> None:
    path = store_path()
    tmp = path + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"credentials": records}, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def add_credential(credential_id: bytes, cose_bytes: bytes, alg: Any, sign_count: int,
                   label: str = "") -> dict[str, Any]:
    record = {
        "id": "pk_" + secrets.token_hex(6),
        "credential_id": b64u_encode(credential_id),
        "public_key": b64u_encode(cose_bytes),
        "alg": alg,
        "sign_count": int(sign_count),
        "label": (label or "")[:80],
        "created_at": time.time(),
        "last_used": None,
    }
    with _LOCK:
        records = _load()
        records.append(record)
        _save(records)
    return dict(record)


def find(credential_id: bytes) -> dict[str, Any] | None:
    target = b64u_encode(credential_id)
    with _LOCK:
        for rec in _load():
            if hmac.compare_digest(str(rec.get("credential_id", "")), target):
                return dict(rec)
    return None


def touch(credential_id: bytes, sign_count: int) -> None:
    target = b64u_encode(credential_id)
    with _LOCK:
        records = _load()
        for rec in records:
            if hmac.compare_digest(str(rec.get("credential_id", "")), target):
                rec["sign_count"] = int(sign_count)
                rec["last_used"] = time.time()
                _save(records)
                return


def list_credentials() -> list[dict[str, Any]]:
    return [
        {"id": r.get("id"), "label": r.get("label"), "alg": r.get("alg"),
         "created_at": r.get("created_at"), "last_used": r.get("last_used")}
        for r in _load()
    ]


def credential_ids() -> list[str]:
    return [str(r.get("credential_id", "")) for r in _load()]


def public_key(record: dict[str, Any]) -> dict[str, Any]:
    return parse_cose_key(b64u_decode(str(record["public_key"])))


def revoke(credential_id: str) -> bool:
    with _LOCK:
        records = _load()
        kept = [r for r in records if r.get("id") != credential_id]
        if len(kept) == len(records):
            return False
        _save(kept)
        return True


def has_credentials() -> bool:
    return bool(_load())


def file_permissions_ok() -> bool:
    path = store_path()
    if not os.path.isfile(path):
        return True
    return stat.S_IMODE(os.stat(path).st_mode) == 0o600
