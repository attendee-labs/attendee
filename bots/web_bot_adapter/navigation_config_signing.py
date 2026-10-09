"""Ed25519 signing for navigation configs.

Remote navigation configs are only trusted if they carry a valid signature from one of
NAVIGATION_CONFIG_PUBLIC_KEYS. The base64 signature lives in the config's top-level "signature"
attribute and covers the config's filename plus a canonical JSON serialization of everything
else, so formatting changes don't invalidate it and a signed config can't be served in place of
a different one.

This module only depends on `cryptography` so it can be run directly as a script:

    python bots/web_bot_adapter/navigation_config_signing.py generate-key
    python bots/web_bot_adapter/navigation_config_signing.py sign [config.json ...]
    python bots/web_bot_adapter/navigation_config_signing.py verify [config.json ...]
"""

import argparse
import base64
import getpass
import json
import os
import re
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

# Base64-encoded raw Ed25519 public keys. Any of them may sign a config, which allows key rotation:
# add the new key, deploy, re-sign every config with the new key, then remove the old key.
NAVIGATION_CONFIG_PUBLIC_KEYS = [
    "fvTAK4xAttpSs1bquYobgb1XGKKIbzHQZClBcT+0NTY=",
]

SIGNATURE_ATTRIBUTE = "signature"
NAVIGATION_CONFIGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "navigation_configs")
DEFAULT_PRIVATE_KEY_PATH = os.path.join(os.path.expanduser("~"), ".config", "attendee", "navigation_config_signing_key.pem")


def _signed_message(config_filename, config):
    unsigned_config = {key: value for key, value in config.items() if key != SIGNATURE_ATTRIBUTE}
    canonical_json = json.dumps(unsigned_config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return config_filename.encode("utf-8") + b"\n" + canonical_json.encode("utf-8")


def verify_navigation_config_signature(config_filename, config, public_keys=None):
    """Raises InvalidSignature unless config (a parsed dict) has a valid signature from one of the public keys."""
    public_keys = NAVIGATION_CONFIG_PUBLIC_KEYS if public_keys is None else public_keys
    signature_b64 = config.get(SIGNATURE_ATTRIBUTE)
    if not isinstance(signature_b64, str):
        raise InvalidSignature(f"Navigation config {config_filename} has no signature")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except ValueError as e:
        raise InvalidSignature(f"Signature for navigation config {config_filename} is not valid base64") from e

    message = _signed_message(config_filename, config)
    for public_key_b64 in public_keys:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        try:
            public_key.verify(signature, message)
            return
        except InvalidSignature:
            continue
    raise InvalidSignature(f"Signature for navigation config {config_filename} does not match any trusted public key")


def sign_navigation_config(private_key, config_filename, config):
    """Returns a copy of config (a parsed dict) with its "signature" attribute set, placed last."""
    signed_config = {key: value for key, value in config.items() if key != SIGNATURE_ATTRIBUTE}
    signed_config[SIGNATURE_ATTRIBUTE] = base64.b64encode(private_key.sign(_signed_message(config_filename, signed_config))).decode("ascii")
    return signed_config


def write_signature_into_config_text(config_text, signature_b64):
    """Sets the top-level signature in config_text while leaving the rest of its formatting untouched."""
    config = json.loads(config_text)
    if SIGNATURE_ATTRIBUTE in config:
        new_text, count = re.subn(rf'("{SIGNATURE_ATTRIBUTE}"\s*:\s*)"[^"]*"', lambda m: f'{m.group(1)}"{signature_b64}"', config_text)
        if count != 1:
            raise ValueError(f'Expected exactly one "{SIGNATURE_ATTRIBUTE}" attribute, found {count}')
    else:
        closing_brace = config_text.rindex("}")
        body = config_text[:closing_brace].rstrip()
        separator = "" if body.endswith("{") else ","
        new_text = f'{body}{separator}\n  "{SIGNATURE_ATTRIBUTE}": "{signature_b64}"\n{config_text[closing_brace:]}'

    new_config = json.loads(new_text)
    if new_config.get(SIGNATURE_ATTRIBUTE) != signature_b64 or {k: v for k, v in new_config.items() if k != SIGNATURE_ATTRIBUTE} != {k: v for k, v in config.items() if k != SIGNATURE_ATTRIBUTE}:
        raise ValueError("Failed to write the signature without changing the rest of the config")
    return new_text


def public_key_b64(private_key):
    raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def _load_private_key(path):
    with open(path, "rb") as f:
        pem = f.read()
    try:
        private_key = serialization.load_pem_private_key(pem, password=None)
    except TypeError:
        passphrase = getpass.getpass(f"Passphrase for {path}: ").encode("utf-8")
        private_key = serialization.load_pem_private_key(pem, password=passphrase)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 private key")
    return private_key


def _config_paths(paths):
    if paths:
        return paths
    return sorted(os.path.join(NAVIGATION_CONFIGS_DIR, f) for f in os.listdir(NAVIGATION_CONFIGS_DIR) if f.endswith(".json"))


def _generate_key_command(args):
    if os.path.exists(args.key):
        print(f"Refusing to overwrite existing key at {args.key}", file=sys.stderr)
        return 1

    if args.no_passphrase:
        encryption = serialization.NoEncryption()
    else:
        passphrase = getpass.getpass("Passphrase for the new key (leave empty for none): ")
        if passphrase and passphrase != getpass.getpass("Repeat passphrase: "):
            print("Passphrases do not match", file=sys.stderr)
            return 1
        encryption = serialization.BestAvailableEncryption(passphrase.encode("utf-8")) if passphrase else serialization.NoEncryption()

    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
    os.makedirs(os.path.dirname(os.path.abspath(args.key)), mode=0o700, exist_ok=True)
    fd = os.open(args.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)

    print(f"Wrote private key to {args.key}")
    print("Add this public key to NAVIGATION_CONFIG_PUBLIC_KEYS:")
    print(public_key_b64(private_key))
    return 0


def _sign_command(args):
    private_key = _load_private_key(args.key)
    if public_key_b64(private_key) not in NAVIGATION_CONFIG_PUBLIC_KEYS:
        print("Warning: this key's public key is not in NAVIGATION_CONFIG_PUBLIC_KEYS, so bots will reject these signatures", file=sys.stderr)

    for path in _config_paths(args.configs):
        with open(path) as f:
            config_text = f.read()
        signed_config = sign_navigation_config(private_key, os.path.basename(path), json.loads(config_text))
        new_config_text = write_signature_into_config_text(config_text, signed_config[SIGNATURE_ATTRIBUTE])
        with open(path, "w") as f:
            f.write(new_config_text)
        print(f"Signed {path}")
    return 0


def _verify_command(args):
    failures = 0
    for path in _config_paths(args.configs):
        try:
            with open(path) as f:
                config = json.load(f)
            verify_navigation_config_signature(os.path.basename(path), config)
        except (OSError, ValueError, InvalidSignature) as e:
            failures += 1
            print(f"FAIL {path}: {e}", file=sys.stderr)
            continue
        print(f"OK   {path}")

    if failures:
        print(f"\n{failures} navigation config(s) have a missing or invalid signature. Re-sign them with: python {os.path.relpath(__file__)} sign", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sign and verify navigation configs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate-key", help="Generate a new Ed25519 signing key")
    generate_parser.add_argument("--key", default=DEFAULT_PRIVATE_KEY_PATH, help=f"Where to write the private key (default: {DEFAULT_PRIVATE_KEY_PATH})")
    generate_parser.add_argument("--no-passphrase", action="store_true", help="Don't prompt for a passphrase; store the key unencrypted")
    generate_parser.set_defaults(func=_generate_key_command)

    sign_parser = subparsers.add_parser("sign", help="Set the signature attribute in configs (defaults to every config)")
    sign_parser.add_argument("--key", default=DEFAULT_PRIVATE_KEY_PATH, help=f"Private key path (default: {DEFAULT_PRIVATE_KEY_PATH})")
    sign_parser.add_argument("configs", nargs="*")
    sign_parser.set_defaults(func=_sign_command)

    verify_parser = subparsers.add_parser("verify", help="Check config signatures against the trusted public keys (defaults to every config)")
    verify_parser.add_argument("configs", nargs="*")
    verify_parser.set_defaults(func=_verify_command)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
