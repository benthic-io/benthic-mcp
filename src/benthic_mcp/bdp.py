import asyncio
import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator, FormatChecker

from benthic_mcp.config import Settings
from benthic_mcp.errors import CatalogError, UpstreamError


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    collections: dict[str, dict[str, Any]]
    manifests: dict[str, dict[str, Any]]
    warnings: tuple[str, ...] = ()


class BdpRepository:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._root_url = settings.bdp_root.rstrip("/") + "/"
        self._root = urlparse(self._root_url)
        self._cache_path = settings.cache_dir / "verified-catalog.json"

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def load(self, force: bool = False) -> CatalogSnapshot:
        async with self._lock:
            cache = await self._load_cache()
            now = time.time()

            if cache is not None:
                age = max(0.0, now - self._cache_path.stat().st_mtime)
                if not force and age <= self.settings.cache_ttl_seconds:
                    try:
                        return self._verify_snapshot(cache)
                    except CatalogError:
                        pass

            try:
                snapshot = await self._fetch_snapshot()
                await self._write_cache(snapshot)
                return snapshot
            except (CatalogError, UpstreamError, httpx.HTTPError, json.JSONDecodeError) as exc:
                if cache is None:
                    raise CatalogError(f"Unable to load a verified BDP catalog: {exc}") from exc
                age = max(0.0, now - self._cache_path.stat().st_mtime)
                if age > self.settings.max_cache_age_seconds:
                    raise CatalogError("The verified BDP cache is too old and refresh failed") from exc
                warning = f"Using verified BDP cache from {int(age)} seconds ago after refresh failed: {exc}"
                verified = self._verify_snapshot(cache)
                return CatalogSnapshot(
                    collections=verified.collections,
                    manifests=verified.manifests,
                    warnings=(*verified.warnings, warning),
                )

    async def _get_json(self, url: str) -> dict[str, Any]:
        self._validate_url(url)
        client = await self._get_client()
        try:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise UpstreamError(f"Failed to fetch {url}: {exc}") from exc
        if not isinstance(data, dict):
            raise CatalogError(f"Expected a JSON object from {url}")
        return data

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                follow_redirects=False,
                headers={"User-Agent": self.settings.user_agent, "Accept": "application/json"},
            )
        return self._client

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        root_path = self._root.path.rstrip("/") + "/"
        if parsed.scheme != self._root.scheme or parsed.netloc != self._root.netloc:
            raise CatalogError(f"BDP URL must use the configured origin: {url}")
        if not parsed.path.startswith(root_path):
            raise CatalogError(f"BDP URL must be under the configured root: {url}")

    async def _fetch_snapshot(self) -> CatalogSnapshot:
        collection_schema = await self._get_json(f"{self._root_url}v1/collection.schema.json")
        manifest_schema = await self._get_json(f"{self._root_url}v1/manifest.schema.json")
        Draft202012Validator.check_schema(collection_schema)
        Draft202012Validator.check_schema(manifest_schema)
        collection_validator = Draft202012Validator(collection_schema, format_checker=FormatChecker())
        manifest_validator = Draft202012Validator(manifest_schema, format_checker=FormatChecker())

        collections: dict[str, dict[str, Any]] = {}
        manifests: dict[str, dict[str, Any]] = {}

        for collection_name in self.settings.collections:
            collection_url = f"{self._root_url}{collection_name}/collection.json"
            collection = await self._get_json(collection_url)
            collection_validator.validate(collection)
            if collection.get("collection_name") != collection_name:
                raise CatalogError(f"Collection name mismatch in {collection_url}")
            verify_signed_document(collection, self.settings.trusted_keys)
            collections[collection_name] = collection

            members = collection.get("members")
            if not isinstance(members, list):
                raise CatalogError(f"Collection {collection_name} has no member list")

            for member in members:
                if not isinstance(member, dict):
                    raise CatalogError(f"Collection {collection_name} contains an invalid member")
                dataset_name = member.get("dataset_name")
                manifest_url = member.get("manifest_url")
                if not isinstance(dataset_name, str) or not isinstance(manifest_url, str):
                    raise CatalogError(f"Collection {collection_name} contains an incomplete member")
                manifest = await self._get_json(manifest_url)
                manifest_validator.validate(manifest)
                if manifest.get("dataset_name") != dataset_name:
                    raise CatalogError(f"Dataset name mismatch in {manifest_url}")
                manifest_hash = verify_signed_document(manifest, self.settings.trusted_keys)
                if manifest_hash != member.get("payload_hash"):
                    raise CatalogError(f"Manifest hash does not match the collection pin: {manifest_url}")
                manifests[dataset_name] = manifest

        return CatalogSnapshot(collections=collections, manifests=manifests)

    def _verify_snapshot(self, data: dict[str, Any]) -> CatalogSnapshot:
        collections = data.get("collections")
        manifests = data.get("manifests")
        if not isinstance(collections, dict) or not isinstance(manifests, dict):
            raise CatalogError("Cached BDP data has an invalid shape")

        for collection_name, collection in collections.items():
            if not isinstance(collection, dict) or collection.get("collection_name") != collection_name:
                raise CatalogError("Cached collection data is inconsistent")
            verify_signed_document(collection, self.settings.trusted_keys)
            members = collection.get("members", [])
            if not isinstance(members, list):
                raise CatalogError("Cached collection members are invalid")
            for member in members:
                if not isinstance(member, dict):
                    raise CatalogError("Cached collection member is invalid")
                dataset_name = member.get("dataset_name")
                manifest = manifests.get(dataset_name)
                if not isinstance(manifest, dict):
                    raise CatalogError(f"Cached manifest is missing for {dataset_name}")
                manifest_hash = verify_signed_document(manifest, self.settings.trusted_keys)
                if manifest_hash != member.get("payload_hash"):
                    raise CatalogError(f"Cached manifest hash mismatch for {dataset_name}")

        selected_collections = {name: collections[name] for name in self.settings.collections if name in collections}
        selected_manifests: dict[str, dict[str, Any]] = {}
        for collection in selected_collections.values():
            for member in collection.get("members", []):
                dataset_name = member["dataset_name"]
                selected_manifests[dataset_name] = manifests[dataset_name]

        if set(selected_collections) != set(self.settings.collections):
            raise CatalogError("Cached BDP data does not contain every configured collection")

        return CatalogSnapshot(collections=selected_collections, manifests=selected_manifests)

    async def _load_cache(self) -> dict[str, Any] | None:
        if not self._cache_path.exists():
            return None
        try:
            data = await asyncio.to_thread(self._read_cache)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _read_cache(self) -> dict[str, Any]:
        with self._cache_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise json.JSONDecodeError("cache root must be an object", "", 0)
        return data

    async def _write_cache(self, snapshot: CatalogSnapshot) -> None:
        data = {"collections": snapshot.collections, "manifests": snapshot.manifests}
        await asyncio.to_thread(self._write_cache_file, data)

    def _write_cache_file(self, data: dict[str, Any]) -> None:
        self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._cache_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"), ensure_ascii=True)
            handle.flush()
        temporary.replace(self._cache_path)


def verify_signed_document(document: dict[str, Any], trusted_keys: tuple[str, ...]) -> str:
    signature = document.get("cryptographic_signature")
    if not isinstance(signature, dict):
        raise CatalogError("BDP document has no signature block")
    if signature.get("algorithm") != "EdDSA":
        raise CatalogError("Unsupported BDP signature algorithm")
    if signature.get("canonicalization", "RFC8785") != "RFC8785":
        raise CatalogError("Unsupported BDP canonicalization")
    if signature.get("hash_algorithm", "SHA-256") != "SHA-256":
        raise CatalogError("Unsupported BDP hash algorithm")

    author_key = document.get("author_pubkey")
    if not isinstance(author_key, str) or author_key not in trusted_keys:
        raise CatalogError("BDP document author key is not trusted")

    payload_hash = signature.get("payload_hash")
    signature_base64 = signature.get("signature_base64")
    if not isinstance(payload_hash, str) or not isinstance(signature_base64, str):
        raise CatalogError("BDP signature block is incomplete")

    payload = {key: value for key, value in document.items() if key != "cryptographic_signature"}
    try:
        canonical = rfc8785.dumps(payload)
        digest = hashlib.sha256(canonical).digest()
        if digest.hex() != payload_hash:
            raise CatalogError("BDP payload hash does not match its canonical content")
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(author_key, validate=True))
        public_key.verify(base64.b64decode(signature_base64, validate=True), digest)
    except (ValueError, InvalidSignature) as exc:
        raise CatalogError("BDP signature verification failed") from exc

    return payload_hash
