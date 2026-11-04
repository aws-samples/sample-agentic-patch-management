"""CVE-scoped installs: patch only the packages a CVE affects.

AWS-RunPatchBaseline's InstallOverrideList names the patches to install. On
Linux it installs listed packages whether or not the baseline approves them,
and only as updates to packages already installed. Ids are written as
"{name}.{arch}" with no version, so each package is upgraded to the latest
available build, which includes the fix.

Whoever can write install-overrides/ decides what installs, so the file is
content-addressed, its package list is part of the approved plan, and the
runtime rewrites it from that list just before a run starts.
"""

import hashlib
import logging
import re
from typing import Dict, List, Optional

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

OVERRIDE_PREFIX = "install-overrides/"
# Package names and architectures as RPM and Inspector write them. Anything
# else (quotes, spaces, wildcards, newlines) is dropped, so a crafted package
# name can't widen the list or break out of the YAML.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
_OS_MANAGER = "OS"


def _s3():
    from helper.tools._shared import _s3 as shared_s3
    return shared_s3()


def _bucket() -> str:
    from helper.tools._shared import _get_compliance_bucket_name
    return _get_compliance_bucket_name()


# Findings read for one CVE; a CVE with more is refused rather than scoped from part of the list.
FINDINGS_LIMIT = 5000


def _find_vulnerabilities(**kwargs) -> dict:
    from helper.tools.vulnerability_tools import _find_vulnerabilities as find
    return find(**kwargs)


def _package_id(pkg: dict) -> Optional[str]:
    # Inspector writes architectures in upper case (X86_64); RPM ids use lower case.
    name, arch = pkg.get("name") or "", (pkg.get("arch") or "").lower()
    pid = f"{name}.{arch}" if arch else name
    return pid if _SAFE_ID.match(pid) and _SAFE_ID.match(name) else None


def cve_packages(cve_id: str, instance_ids: Optional[List[str]] = None,
                 account_id: Optional[str] = None) -> Dict[str, list]:
    """OS packages to upgrade for a CVE on the given instances.

    instance_ids=None means every affected instance; an empty list means none.

    Returns:
        {packages: sorted ids, not_patchable: language packages Patch Manager can't update,
         instances: affected target instances, truncated: the findings list was cut off}
    """
    result = _find_vulnerabilities(cve_id=cve_id, account_id=account_id, limit=FINDINGS_LIMIT)
    targets = None if instance_ids is None else set(instance_ids)
    packages, not_patchable, instances = set(), set(), set()
    for f in result.get("findings") or []:
        if f.get("cve_id") != cve_id:
            continue
        hit = set(f.get("affected_resources") or [])
        if targets is not None:
            hit &= targets
        if not hit:
            continue
        instances |= hit
        for pkg in f.get("packages") or []:
            if (pkg.get("package_manager") or _OS_MANAGER) != _OS_MANAGER:
                not_patchable.add(f"{pkg.get('name')} ({pkg.get('package_manager')})")
                continue
            pid = _package_id(pkg)
            if pid:
                packages.add(pid)
            else:
                logger.warning(f"[CVE_SCOPE] dropped unsafe package id {pkg.get('name')!r} for {cve_id}")
    # A Region that couldn't be read hides that Region's findings: its instances would look
    # unaffected and its packages would be missing from the list (2 Nov review, H3).
    unreadable = list(result.get("partial_failures") or [])
    if result.get("error"):
        unreadable.append(str(result["error"])[:200])
    return {"packages": sorted(packages), "not_patchable": sorted(not_patchable), "instances": sorted(instances),
            "truncated": bool(result.get("truncated")), "unreadable": unreadable}


def override_yaml(packages: List[str]) -> str:
    ids = sorted(set(packages))
    if not ids or not all(_SAFE_ID.match(p) for p in ids):
        raise ValueError("install override needs at least one safe package id")
    return "patches:\n" + "".join(f"  - id: '{p}'\n" for p in ids)


def write_install_override(packages: List[str]) -> str:
    """Write the override file for these packages and return its S3 URL (same packages, same URL)."""
    body = override_yaml(packages)
    key = f"{OVERRIDE_PREFIX}{hashlib.sha256(body.encode()).hexdigest()}.yaml"
    bucket = _bucket()
    # Created once; after that, check the stored file still holds this list.
    try:
        _s3().put_object(Bucket=bucket, Key=key, Body=body.encode(), ContentType="application/yaml",
                         IfNoneMatch="*")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("PreconditionFailed", "ConditionalRequestConflict"):
            raise
        stored = _s3().get_object(Bucket=bucket, Key=key)["Body"].read().decode()
        if stored != body:
            raise ValueError(f"install list {key} changed after it was written") from None
    return f"s3://{bucket}/{key}"
