"""Create a local test CA, client certificates, and token keys.

Certificates are valid for 48 hours. Nothing here is a real certificate.
The private token keys stay in the output directory the caller chooses
(use a tmpfs such as /dev/shm). They are not copied into the API image.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.x509.oid import NameOID


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _signed(
    subject: x509.Name,
    issuer: x509.Name,
    subject_key: rsa.RSAPrivateKey,
    issuer_key: rsa.RSAPrivateKey,
    *,
    is_ca: bool,
    san: x509.SubjectAlternativeName | None,
) -> x509.Certificate:
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=48))
    )
    if is_ca:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=True, path_length=0), critical=True
        )
    else:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
    if san is not None:
        builder = builder.add_extension(san, critical=False)
    return builder.sign(issuer_key, SHA256())


def _client(
    out: Path, ca_key: rsa.RSAPrivateKey, ca_name: x509.Name, common_name: str
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = _signed(
        _name(common_name),
        ca_name,
        key,
        ca_key,
        is_ca=False,
        san=None,
    )
    _write_key(out / f"{common_name}.key", key)
    _write_cert(out / f"{common_name}.crt", cert)


def _server(
    out: Path,
    ca_key: rsa.RSAPrivateKey,
    ca_name: x509.Name,
    hostname: str,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = _signed(
        _name(hostname),
        ca_name,
        key,
        ca_key,
        is_ca=False,
        san=x509.SubjectAlternativeName(
            [x509.DNSName(hostname), x509.IPAddress(IPv4Address("127.0.0.1"))]
        ),
    )
    _write_key(out / f"{hostname}.key", key)
    _write_cert(out / f"{hostname}.crt", cert)


def _token_key(out: Path, name: str) -> None:
    private = ed25519.Ed25519PrivateKey.generate()
    out.joinpath(f"{name}.token.key").write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    out.joinpath(f"{name}.token.pub").write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = _name("sentica-local-test-ca")
    ca_cert = _signed(ca_name, ca_name, ca_key, ca_key, is_ca=True, san=None)
    _write_key(out / "ca.key", ca_key)
    _write_cert(out / "ca.crt", ca_cert)

    _client(out, ca_key, ca_name, "tenant_a")
    _client(out, ca_key, ca_name, "tenant_b")
    _server(out, ca_key, ca_name, "pgbouncer-a")
    _server(out, ca_key, ca_name, "pgbouncer-b")
    _token_key(out, "tenant-a")
    _token_key(out, "tenant-b")
    # Touch a file so an empty directory cannot be mistaken for a finished CA.
    (out / "READY").write_text("local-test-ca\n", encoding="utf-8")


if __name__ == "__main__":
    main()
