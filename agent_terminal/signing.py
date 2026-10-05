"""Release signatures with OpenSSH (``ssh-keygen -Y``): no new dependencies.

The dev machine signs each release tarball with its SSH key (namespace
``agent-terminal``). A target lists the trusted public key(s) in
``~/.config/agent-terminal/allowed_signers``; once that file exists, an update
without a valid signature is refused. (sha256 alone only detects corruption: the
manifest travels with the tarball, so it cannot authenticate it.)
"""
from __future__ import annotations

import os
import shutil
import subprocess

NAMESPACE = "agent-terminal"
PRINCIPAL = "agent-terminal-release"


class SignatureError(RuntimeError):
    pass


def allowed_signers_line(public_key_text: str) -> str:
    key = " ".join(public_key_text.split()[:2])  # "ssh-ed25519 AAAA..." (drop the comment)
    return f'{PRINCIPAL} namespaces="{NAMESPACE}" {key}'


def sign(path: str, private_key: str) -> str:
    if not shutil.which("ssh-keygen"):
        raise SignatureError("ssh-keygen not found")
    sig = path + ".sig"
    if os.path.exists(sig):  # ssh-keygen keeps an existing .sig silently: a stale one must never pass as new
        os.unlink(sig)
    out = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", private_key, "-n", NAMESPACE, path],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if out.returncode != 0 or not os.path.exists(sig):
        raise SignatureError(out.stderr.strip() or "signing failed")
    return sig


def verify(path: str, signature: str, allowed_signers: str) -> None:
    if not shutil.which("ssh-keygen"):
        raise SignatureError("ssh-keygen not found: cannot verify the release signature")
    if not os.path.isfile(signature):
        raise SignatureError(f"missing signature file {os.path.basename(signature)} "
                             "(a trusted signer is configured, unsigned releases are refused)")
    with open(path, "rb") as fh:
        out = subprocess.run(
            ["ssh-keygen", "-Y", "verify", "-f", allowed_signers, "-I", PRINCIPAL,
             "-n", NAMESPACE, "-s", signature],
            stdin=fh, capture_output=True, text=True)
    if out.returncode != 0:
        raise SignatureError("signature verification FAILED: " + (out.stderr.strip() or out.stdout.strip()))
