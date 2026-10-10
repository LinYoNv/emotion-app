"""Hash-locked local catalog for administrator-owned reaction assets (REQ-021 Q6)."""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CATALOG_CONTRACT = "ops.q6.owned_reaction_assets.v1"
ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MAX_ASSET_BYTES = 10 * 1024 * 1024
MAX_ASSETS = 96
MAX_TAGS = 12
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_TOKEN_RE = re.compile(r"[a-z0-9_\u4e00-\u9fff]{1,48}", re.I)


@dataclass(frozen=True)
class OwnedReactionAsset:
    asset_id: str
    path: Path
    tags: tuple[str, ...]
    meme_only: bool


class OwnedReactionAssetCatalog:
    """A source-limited catalog; it never imports chat or vision-cache assets."""

    def __init__(self, data_dir: str | Path) -> None:
        self._root = (Path(data_dir) / "owned_reaction_assets").resolve()

    @staticmethod
    def _text(value: Any, limit: int = 160) -> str:
        return " ".join(str(value or "").split())[:limit]

    @staticmethod
    def _bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None or str(value).strip() == "":
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}

    def _resolve_path(self, value: Any) -> Path | None:
        raw = self._text(value, 240).replace("\\", "/")
        if not raw or raw.startswith(("/", "//")) or ":" in raw or ".." in raw.split("/"):
            return None
        try:
            path = (self._root / raw).resolve()
            path.relative_to(self._root)
        except (OSError, ValueError):
            return None
        return path

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @classmethod
    def _tags(cls, value: Any) -> tuple[str, ...]:
        raw = value if isinstance(value, (list, tuple, set)) else re.split(r"[,，;；|\s]+", str(value or ""))
        tags: list[str] = []
        for item in raw:
            tag = cls._text(item, 48).casefold()
            if not tag or "/" in tag or "\\" in tag or ":" in tag:
                continue
            if not _TOKEN_RE.fullmatch(tag) or tag in tags:
                continue
            tags.append(tag)
            if len(tags) >= MAX_TAGS:
                break
        return tuple(tags)

    def validate_entry(self, raw: Any) -> tuple[OwnedReactionAsset | None, str]:
        if not isinstance(raw, dict):
            return None, "entry_not_object"
        asset_id = self._text(raw.get("id") or raw.get("asset_id"), 80)
        digest = self._text(raw.get("sha256"), 80).lower()
        tags = self._tags(raw.get("tags"))
        path = self._resolve_path(raw.get("file") or raw.get("relative_path"))
        if not _ASSET_ID_RE.fullmatch(asset_id):
            return None, "invalid_id"
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            return None, "invalid_hash"
        if not tags:
            return None, "missing_tags"
        if path is None:
            return None, "outside_managed_directory"
        try:
            if not path.is_file():
                return None, "file_missing"
            if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                return None, "unsupported_extension"
            if path.stat().st_size > MAX_ASSET_BYTES:
                return None, "file_too_large"
            if not secrets.compare_digest(self._sha256(path), digest):
                return None, "hash_mismatch"
        except OSError:
            return None, "file_unreadable"
        return OwnedReactionAsset(
            asset_id=asset_id,
            path=path,
            tags=tags,
            meme_only=self._bool(raw.get("meme_only"), True),
        ), "ok"

    def inspect(self, entries: Any) -> dict[str, Any]:
        raw_entries = entries if isinstance(entries, list) else []
        assets: list[OwnedReactionAsset] = []
        projection: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw in raw_entries[:MAX_ASSETS]:
            asset, status = self.validate_entry(raw)
            raw_id = self._text(raw.get("id") if isinstance(raw, dict) else "", 80)
            asset_id = raw_id if _ASSET_ID_RE.fullmatch(raw_id) else ""
            if asset and asset.asset_id in seen_ids:
                asset, status = None, "duplicate_id"
            if asset:
                seen_ids.add(asset.asset_id)
                assets.append(asset)
                asset_id = asset.asset_id
                projection.append({
                    "id": asset_id,
                    "tags": list(asset.tags),
                    "meme_only": asset.meme_only,
                    "status": "ok",
                    "valid": True,
                })
            else:
                projection.append({"id": asset_id, "tags": [], "meme_only": True, "status": status, "valid": False})
        return {"assets": assets, "items": projection}

    @staticmethod
    def _query_tokens(value: Any) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_TOKEN_RE.findall(str(value or "").casefold())))[:24]

    def find(self, entries: Any, *, query: str, search_context: str = "", meme_only: bool = True) -> tuple[OwnedReactionAsset | None, str, float]:
        inspected = self.inspect(entries)
        query_tokens = self._query_tokens(query)
        context_tokens = self._query_tokens(search_context)
        if not query_tokens:
            return None, "empty_query", 0.0
        candidates: list[tuple[float, OwnedReactionAsset]] = []
        for asset in inspected["assets"]:
            if meme_only and not asset.meme_only:
                continue
            score = 0.0
            for tag in asset.tags:
                if tag in query_tokens:
                    score += 10.0
                elif any(tag in token or token in tag for token in query_tokens if len(token) >= 2):
                    score += 5.0
                if tag in context_tokens:
                    score += 1.0
            if score > 0:
                candidates.append((score, asset))
        if not candidates:
            return None, "not_found", 0.0
        candidates.sort(key=lambda item: (-item[0], item[1].asset_id))
        score, asset = candidates[0]
        return asset, "ok", min(1.0, score / 10.0)

    def resolve(self, entries: Any, asset_id: Any) -> OwnedReactionAsset | None:
        requested = self._text(asset_id, 80)
        if not _ASSET_ID_RE.fullmatch(requested):
            return None
        return next((asset for asset in self.inspect(entries)["assets"] if asset.asset_id == requested), None)

    def public_projection(self, entries: Any) -> dict[str, Any]:
        inspected = self.inspect(entries)
        return {
            "contract": CATALOG_CONTRACT,
            "managed_directory": "owned_reaction_assets",
            "items": inspected["items"],
            "max_assets": MAX_ASSETS,
        }
