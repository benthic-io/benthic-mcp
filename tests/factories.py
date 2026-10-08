from copy import deepcopy
from typing import Any

import rfc8785
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def public_key_base64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return __import__("base64").b64encode(raw).decode("ascii")


def sign_document(document: dict[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    signed = deepcopy(document)
    payload = {key: value for key, value in signed.items() if key != "cryptographic_signature"}
    digest = __import__("hashlib").sha256(rfc8785.dumps(payload)).digest()
    signed["cryptographic_signature"] = {
        "algorithm": "EdDSA",
        "canonicalization": "RFC8785",
        "hash_algorithm": "SHA-256",
        "payload_hash": digest.hex(),
        "signature_base64": __import__("base64").b64encode(private_key.sign(digest)).decode("ascii"),
        "signed_at": "2026-09-24T00:00:00Z",
    }
    return signed


def make_manifest(
    private_key: Ed25519PrivateKey,
    dataset: str,
    relations: list[dict[str, Any]],
    base_url: str,
) -> dict[str, Any]:
    return sign_document(
        {
            "protocol_version": "1.0.0",
            "author_identity": "test@example.com",
            "author_pubkey": public_key_base64(private_key),
            "dataset_name": dataset,
            "collection": "ngopen",
            "title": dataset,
            "etl_provenance": {
                "repository_url": "https://github.com/example/test",
                "commit_hash": "a" * 40,
                "migration_status": "migrated",
            },
            "schema_definition": relations,
            "endpoints": [
                {
                    "transport_type": "postgrest_api",
                    "base_url": base_url,
                    "meta": {"accept_profile": "public", "max_rows": 1000, "requires_auth": False},
                }
            ],
        },
        private_key,
    )


def make_collection(
    private_key: Ed25519PrivateKey,
    manifests: dict[str, dict[str, Any]],
    join_paths: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    members = [
        {
            "dataset_name": dataset,
            "manifest_url": f"https://benthic.io/bdp/ngopen/{dataset}/manifest.json",
            "payload_hash": manifest["cryptographic_signature"]["payload_hash"],
            "title": dataset,
        }
        for dataset, manifest in manifests.items()
    ]
    return sign_document(
        {
            "protocol_version": "1.0.0",
            "author_identity": "test@example.com",
            "author_pubkey": public_key_base64(private_key),
            "collection_name": "ngopen",
            "title": "NGOpen",
            "members": members,
            "join_paths": join_paths or [],
        },
        private_key,
    )


def relation(
    name: str,
    columns: list[tuple[str, str]],
    *,
    queryable: bool = True,
    description: str | None = None,
    non_nullable: tuple[str, ...] = (),
    primary_key: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    # non_nullable is explicit because the catalog reads a missing key as nullable, so a fixture that
    # means "cannot be null" has to say so. The production manifest marks primary keys that way, and
    # it changes what a scan refusal is allowed to claim about a count.
    return {
        "name": name,
        "relation_type": "table",
        "provenance": "upstream",
        "queryable": queryable,
        "description": description,
        # primary_key is overridable, and an explicit () means "this relation declares no primary key",
        # which is a real property and not a fixture artefact: usaspending.reporting_agency_overview
        # has none, so an aggregate over it is refused as unreliable unless the filter leaves fewer
        # rows than one page. A fixture that invented a primary key would let guidance about that
        # refusal pass screening while describing a relation the manifest does not contain.
        "primary_key": list(primary_key) if primary_key is not None else ([columns[0][0]] if columns else []),
        "columns": [
            {
                "name": column,
                "type": column_type,
                # Always explicit. The catalog reads a missing key as nullable, so a non-nullable
                # column has to carry `nullable: false` rather than simply omit it - omitting it is
                # what produced a fixture that claimed a primary key could be null.
                "nullable": column not in non_nullable,
            }
            for column, column_type in columns
        ],
    }
