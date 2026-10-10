# -*- coding: utf-8 -*-
from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any


class StoreBackendBase:
    def backend_name(self) -> str:
        raise NotImplementedError

    def load_store(self) -> dict[str, Any]:
        raise NotImplementedError

    def save_store(self, data: dict[str, Any]) -> None:
        raise NotImplementedError

    def save_sections(
        self,
        changed_sections: Mapping[str, tuple[int, Any]],
        deleted_sections: Mapping[str, int],
    ) -> Mapping[str, int]:
        raise NotImplementedError

    def save_snapshot(
        self,
        data: dict[str, Any],
        *,
        minimum_revision: int | None = None,
        deleted_sections: Mapping[str, int] | None = None,
        preserve_tombstones: bool = False,
    ) -> int | None:
        self.save_store(data)
        return None

    def exists(self) -> bool:
        raise NotImplementedError

    def initialize_empty_store(self, default_data: dict[str, Any]) -> None:
        self.save_store(default_data)

    def health_check(self, *, raise_on_error: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    def next_revision(self) -> int:
        return 1

    def deleted_section_revisions(
        self,
        section_names: Collection[str],
    ) -> Mapping[str, int]:
        return {}

    def export_store(self) -> dict[str, Any]:
        return self.load_store()

    def import_store(self, data: dict[str, Any], mode: str = "replace") -> None:
        self.save_store(data)

    def close(self) -> None:
        return None
