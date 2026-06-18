"""Hashed learning-model artifact storage."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx


class ArtifactStore:
    def put(self, name: str, content: bytes) -> tuple[str, str]:
        raise NotImplementedError

    def get(self, uri: str) -> bytes:
        raise NotImplementedError


class LocalArtifactStore(ArtifactStore):
    def __init__(self, root: str):
        self.root = Path(root)

    def put(self, name: str, content: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(content).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{digest}-{Path(name).name}"
        path.write_bytes(content)
        return str(path), digest

    def get(self, uri: str) -> bytes:
        return Path(uri).read_bytes()


class SupabaseArtifactStore(ArtifactStore):
    """Private Supabase Storage adapter using a service-role key."""

    def __init__(self, *, url: str, service_key: str, bucket: str, timeout_seconds: int = 30):
        self.url = url.rstrip("/")
        self.service_key = service_key
        self.bucket = bucket
        self.timeout_seconds = timeout_seconds

    def put(self, name: str, content: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(content).hexdigest()
        object_name = f"{digest}-{Path(name).name}"
        response = httpx.post(
            f"{self.url}/storage/v1/object/{self.bucket}/{object_name}",
            headers={
                "Authorization": f"Bearer {self.service_key}",
                "apikey": self.service_key,
                "Content-Type": "application/octet-stream",
                "x-upsert": "false",
            },
            content=content,
            timeout=self.timeout_seconds,
        )
        if response.status_code not in {200, 201, 409}:
            raise RuntimeError(f"Supabase artifact upload failed: HTTP {response.status_code}")
        return f"supabase://{self.bucket}/{object_name}", digest

    def get(self, uri: str) -> bytes:
        prefix = f"supabase://{self.bucket}/"
        if not uri.startswith(prefix):
            raise ValueError("Artifact URI does not match configured private bucket")
        object_name = uri.removeprefix(prefix)
        response = httpx.get(
            f"{self.url}/storage/v1/object/authenticated/{self.bucket}/{object_name}",
            headers={"Authorization": f"Bearer {self.service_key}", "apikey": self.service_key},
            timeout=self.timeout_seconds,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Supabase artifact download failed: HTTP {response.status_code}")
        return response.content


def build_artifact_store(settings: Any) -> ArtifactStore:
    if bool(getattr(settings, "learning_supabase_storage_enabled", False)):
        if not settings.learning_supabase_url or not settings.learning_supabase_service_key:
            raise RuntimeError("Supabase learning artifact storage requires URL and service key")
        return SupabaseArtifactStore(
            url=settings.learning_supabase_url,
            service_key=settings.learning_supabase_service_key,
            bucket=settings.learning_supabase_bucket,
            timeout_seconds=settings.learning_openai_timeout_seconds,
        )
    return LocalArtifactStore(settings.learning_artifact_dir)
