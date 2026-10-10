"""Pure validation for the shared P6 four-package manifest contract."""
from __future__ import annotations

import hashlib
import json
import re


FOUR_PACKAGE_MANIFEST_SCHEMA = "ops.p6.four_package_manifest.v1"
FOUR_PACKAGE_MANIFEST_REPORT_SCHEMA = "ops.p6.four_package_manifest_report.v1"
FOUR_PACKAGE_IDS = ("companion", "memory", "ops_archive", "peiban")

_REQUIRED_MANIFEST_FIELDS = (
    "schema",
    "package_id",
    "manifest_version",
    "package_fingerprint",
    "compatibility_fingerprint",
)
_MANIFEST_VERSION_PATTERN = re.compile(r"^[1-9]\d*\.[0-9]+$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _contract_payload() -> dict[str, object]:
    return {
        "schema": FOUR_PACKAGE_MANIFEST_SCHEMA,
        "report_schema": FOUR_PACKAGE_MANIFEST_REPORT_SCHEMA,
        "package_ids": list(FOUR_PACKAGE_IDS),
        "required_manifest_fields": list(_REQUIRED_MANIFEST_FIELDS),
    }


def manifest_contract() -> dict[str, object]:
    descriptor = _contract_payload()
    canonical = json.dumps(descriptor, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor["contract_fingerprint"] = hashlib.sha256(canonical).hexdigest()
    return descriptor


def _unverifiable_report(reason_code: str) -> dict[str, object]:
    return {
        "schema": FOUR_PACKAGE_MANIFEST_REPORT_SCHEMA,
        "status": "unverifiable",
        "reason_code": reason_code,
        "expected_package_ids": list(FOUR_PACKAGE_IDS),
    }


def _verified_report() -> dict[str, object]:
    return {
        "schema": FOUR_PACKAGE_MANIFEST_REPORT_SCHEMA,
        "status": "verified",
        "reason_code": "four_package_manifest_verified",
        "expected_package_ids": list(FOUR_PACKAGE_IDS),
    }


def verify_four_package_manifests(manifests: object) -> dict[str, object]:
    """Verify values only; no package object or runtime is inspected."""
    if type(manifests) is not dict:
        return _unverifiable_report("manifest_collection_not_exact_dict")
    for package_id in manifests:
        if type(package_id) is not str or package_id not in FOUR_PACKAGE_IDS:
            return _unverifiable_report("unexpected_package_manifest")
    for package_id in FOUR_PACKAGE_IDS:
        if package_id not in manifests:
            return _unverifiable_report("missing_package_manifest")

    common_compatibility_fingerprint = None
    for package_id in FOUR_PACKAGE_IDS:
        package_manifest = manifests[package_id]
        if type(package_manifest) is not dict:
            return _unverifiable_report("package_manifest_not_exact_dict")
        for field_name in package_manifest:
            if type(field_name) is not str or field_name not in _REQUIRED_MANIFEST_FIELDS:
                return _unverifiable_report("unexpected_manifest_field")
        for field_name in _REQUIRED_MANIFEST_FIELDS:
            if field_name not in package_manifest:
                return _unverifiable_report("missing_manifest_field")
        if type(package_manifest["schema"]) is not str or package_manifest["schema"] != FOUR_PACKAGE_MANIFEST_SCHEMA:
            return _unverifiable_report("manifest_schema_mismatch")
        if type(package_manifest["package_id"]) is not str or package_manifest["package_id"] != package_id:
            return _unverifiable_report("manifest_package_id_mismatch")
        version = package_manifest["manifest_version"]
        if type(version) is not str or _MANIFEST_VERSION_PATTERN.fullmatch(version) is None:
            return _unverifiable_report("manifest_version_invalid")
        package_fingerprint = package_manifest["package_fingerprint"]
        if type(package_fingerprint) is not str or _SHA256_PATTERN.fullmatch(package_fingerprint) is None:
            return _unverifiable_report("package_fingerprint_invalid")
        compatibility_fingerprint = package_manifest["compatibility_fingerprint"]
        if type(compatibility_fingerprint) is not str or _SHA256_PATTERN.fullmatch(compatibility_fingerprint) is None:
            return _unverifiable_report("compatibility_fingerprint_invalid")
        if common_compatibility_fingerprint is None:
            common_compatibility_fingerprint = compatibility_fingerprint
        elif compatibility_fingerprint != common_compatibility_fingerprint:
            return _unverifiable_report("compatibility_fingerprint_mismatch")
    return _verified_report()


__all__ = [
    "FOUR_PACKAGE_IDS",
    "FOUR_PACKAGE_MANIFEST_REPORT_SCHEMA",
    "FOUR_PACKAGE_MANIFEST_SCHEMA",
    "manifest_contract",
    "verify_four_package_manifests",
]
