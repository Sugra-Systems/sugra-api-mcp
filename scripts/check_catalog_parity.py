"""Fail when the bundled endpoint catalog has drifted from an API spec.

The bundle is a build artifact of the Sugra API's OpenAPI document, but nothing
rebuilds it when API routes land, and its `source` field is a free-text label -
so a stale bundle was only discoverable by reading it. That is how a deployed
surface ended up serving a catalog several releases behind while every filter
and search silently returned "nothing found" for the newer operations.

This compares the bundled catalog against a spec and exits non-zero when the
operation_id sets differ, naming what to do about it.

  python scripts/check_catalog_parity.py                     # against the live spec
  python scripts/check_catalog_parity.py --spec path.json    # against a local spec

Exit codes are chosen so the gate cannot die quietly:

  0  operation sets match
  1  DRIFT - the bundle and the spec disagree, or the check could not be run at
     all for a DETERMINISTIC reason (missing local file, 4xx from the spec URL).
     A permanently broken gate must be as loud as a real difference, otherwise a
     typo'd path keeps the build green forever while the catalog rots.
  0  SKIPPED - only for a TRANSIENT fetch failure (connection reset, timeout,
     5xx). An outage must not read as "the catalog drifted"; the scheduled run
     retries it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sugra_api_mcp.catalog.builder import build_catalog_from_openapi  # noqa: E402
from sugra_api_mcp.catalog.models import Catalog  # noqa: E402

DEFAULT_SPEC = "https://sugra.ai/openapi.json"
BUNDLE = REPO_ROOT / "sugra_api_mcp" / "catalog" / "data" / "endpoints.json"


class SpecUnavailable(Exception):
    """The spec could not be fetched for a transient reason (retry later)."""


class SpecMisconfigured(Exception):
    """The spec source is wrong and will stay wrong until someone fixes it."""


def load_bundle(path: Path) -> Catalog:
    return Catalog.from_dict(json.loads(path.read_text(encoding="utf-8")))


def read_spec(source: str) -> bytes:
    """Spec bytes.

    Raises SpecMisconfigured for a deterministic problem (the caller turns that
    into a failure) and SpecUnavailable for a transient one (skipped).
    """
    if source.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(source, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # ONLY 5xx is the server having a bad day. Everything else that
            # reaches here is standing misconfiguration: 4xx says this URL is
            # wrong, and a 3xx surfacing as an error means a redirect loop or a
            # redirect to an unsupported scheme - neither fixes itself, so
            # neither may skip.
            if 500 <= exc.code < 600:
                raise SpecUnavailable(f"{source} returned HTTP {exc.code}") from exc
            raise SpecMisconfigured(f"{source} returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            # urllib wraps certificate-verification failures in URLError, but an
            # expired or untrusted certificate is a standing misconfiguration:
            # treating it as transient would keep every run green while nothing
            # is ever compared.
            if isinstance(exc.reason, ssl.SSLError):
                raise SpecMisconfigured(f"TLS failure for {source}: {exc.reason}") from exc
            raise SpecUnavailable(f"could not fetch {source}: {exc}") from exc
        except (TimeoutError, OSError) as exc:
            raise SpecUnavailable(f"could not fetch {source}: {exc}") from exc
    path = Path(source)
    if not path.exists():
        raise SpecMisconfigured(f"spec not found: {path}")
    return path.read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check bundled catalog against an API spec.")
    parser.add_argument("--spec", default=DEFAULT_SPEC, help="Spec path or http(s) URL.")
    parser.add_argument("--bundle", type=Path, default=BUNDLE)
    args = parser.parse_args()

    bundle = load_bundle(args.bundle)
    print(f"Bundle: {bundle.endpoint_count} operations, source={bundle.source}")
    print(f"        spec_sha256={bundle.spec_sha256 or '(unstamped)'} built_at={bundle.built_at or '-'}")

    try:
        raw = read_spec(args.spec)
    except SpecMisconfigured as exc:
        print(f"FAIL: {exc}")
        print("  The parity gate cannot run against this source. Fix --spec; a gate "
              "that cannot check is not a passing gate.")
        return 1
    except SpecUnavailable as exc:
        print(f"SKIPPED: {exc}")
        print("  Transient - the scheduled parity run will retry.")
        return 0

    # The stamp authenticates the SOURCE BYTES, not the generated catalog, so it
    # must never short-circuit the comparison: an endpoint edited or dropped out
    # of the bundle by hand keeps the stamp intact and would sail through. Always
    # rebuild from the spec and diff the operation sets; the stamp is reported as
    # provenance, never trusted as proof.
    spec_sha = hashlib.sha256(raw).hexdigest()
    stamp_matches = bool(bundle.spec_sha256) and bundle.spec_sha256 == spec_sha

    current = build_catalog_from_openapi(json.loads(raw.decode("utf-8")), source=args.spec)
    missing = sorted(current.operation_ids - bundle.operation_ids)
    extra = sorted(bundle.operation_ids - current.operation_ids)

    # Matching operation IDs are NOT a matching contract. A path, method,
    # parameter, required-body flag or body schema can change while the id stays
    # put, and a client would then call a stale signature against the live API -
    # exactly the failure this gate exists to prevent, so compare the whole
    # generated endpoint, not just its name.
    bundle_by_id = {e.operation_id: e.to_dict() for e in bundle.endpoints}
    current_by_id = {e.operation_id: e.to_dict() for e in current.endpoints}
    changed = sorted(
        op_id for op_id in bundle_by_id.keys() & current_by_id.keys()
        if bundle_by_id[op_id] != current_by_id[op_id]
    )

    if not missing and not extra and not changed:
        print(f"OK: {bundle.endpoint_count} operations match the spec in full "
              f"(ids, paths, parameters, bodies); spec sha256 {spec_sha[:12]}... "
              + ("matches the bundle stamp." if stamp_matches
                 else "differs from the bundle stamp (spec text changed, catalog did not)."))
        return 0

    print(f"DRIFT: bundled catalog does not match {args.spec}")
    if stamp_matches:
        # Worth calling out: the bundle claims to come from exactly this spec, so
        # the difference was introduced after the build.
        print("  NOTE: the bundle's spec stamp matches this spec, so the bundle was "
              "modified after it was built.")
    if missing:
        print(f"  missing from the bundle ({len(missing)}): {', '.join(missing[:20])}"
              + (" ..." if len(missing) > 20 else ""))
    if extra:
        print(f"  not in the spec ({len(extra)}): {', '.join(extra[:20])}"
              + (" ..." if len(extra) > 20 else ""))
    if changed:
        print(f"  same id, changed contract ({len(changed)}):")
        for op_id in changed[:10]:
            fields = sorted(
                key for key in bundle_by_id[op_id].keys() | current_by_id[op_id].keys()
                if bundle_by_id[op_id].get(key) != current_by_id[op_id].get(key)
            )
            print(f"    {op_id}: {', '.join(fields)}")
        if len(changed) > 10:
            print(f"    ... and {len(changed) - 10} more")
    print("  Rebuild the bundle:  python scripts/build_endpoint_catalog.py "
          f"--source {args.spec}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
