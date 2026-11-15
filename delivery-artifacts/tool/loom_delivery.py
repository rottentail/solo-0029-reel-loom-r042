#!/usr/bin/env python3
"""loom_delivery — offline, resumable, hash-pinned delivery tool.

This tool is intentionally independent of the Reel Loom service and its
database. It only needs Python 3.11+ standard library. It turns a *frozen
selection* of a production volume into a self-describing package that a
receiver can validate offline.

Design guarantees
-----------------
1. Pinned, not "latest": ``plan`` hashes every selected byte once. Later
   ``deliver`` runs copy exactly those hashes. If the source file changes
   underneath, the run reports SOURCE_DRIFT and refuses to substitute the
   new revision.
2. Resumable without a second media copy: the destination is the only
   place media bytes live. A file is "complete" only when its full content
   hash matches the plan; partial ``.part`` files are never trusted and are
   re-streamed from source. The tool never stages a full copy in the small
   local workspace.
3. Unplug-tolerant: write failures (including an injected EIO that models a
   drive disappearing mid-copy) end the run cleanly; the next run reuses
   already-verified destination files and continues from the first missing
   or damaged one.
4. No leakage: only paths reachable from the frozen plan are copied.
   Question-bank / private audit material is never enumerated.

Subcommands
-----------
  plan     Freeze a selection from a source catalog into delivery-plan.json
  deliver  Copy / verify the plan onto the (removable) destination volume
  verify   Standalone offline verification of a finished package

Exit codes: 0 ok / fully verified; 2 interrupted (re-run to continue);
3 source drift or missing source (human decision required); 4 corrupt
package that cannot be repaired; 1 any other usage/runtime error.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

CHUNK = 1024 * 1024
PLAN_NAME = "delivery-plan.json"
RECEIPT_NAME = "acceptance-record.json"
VERIFIER_NAME = "offline_verify.py"
MANIFEST_NAME = "package-manifest.json"
PART_SUFFIX = ".part"
SCHEMA = 1

# Exit codes
OK = 0
ERROR = 1
INTERRUPTED = 2
SOURCE_DRIFT = 3
CORRUPT = 4


class DeliveryError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = ERROR):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class VolumeUnplugged(DeliveryError):
    """Raised when the destination behaves like a removed drive."""

    def __init__(self, message: str):
        super().__init__("volume_unplugged", message, INTERRUPTED)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      indent=2, allow_nan=False).encode("utf-8")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DeliveryError("missing_file", f"Required file not found: {path}")
    except json.JSONDecodeError as exc:
        raise DeliveryError("bad_json", f"Invalid JSON in {path}: {exc}")


def write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def cmd_plan(args) -> int:
    catalog = load_json(args.catalog)
    if catalog.get("schema") != 1:
        raise DeliveryError("bad_catalog", "Unsupported source-catalog schema")
    source_root = args.catalog.parent.resolve()
    selected_ids = set(args.delivery) if args.delivery else None

    entries = []
    selected = []
    for delivery in catalog["deliveries"]:
        if selected_ids and delivery["delivery_id"] not in selected_ids:
            continue
        selected.append(delivery["delivery_id"])
        for revision in delivery["revisions"]:
            for item in revision["files"]:
                src = (source_root / item["path"]).resolve()
                # Defensive: never reach outside the production volume.
                if not _is_within(source_root, src):
                    raise DeliveryError("path_escape",
                                        f"Planned path escapes source volume: {item['path']}")
                if not src.is_file():
                    raise DeliveryError("missing_source",
                                        f"Source file vanished before planning: {item['path']}",
                                        SOURCE_DRIFT)
                actual = sha256_file(src)
                if actual != item["sha256"]:
                    raise DeliveryError(
                        "source_changed",
                        f"Catalog digest no longer matches source for {item['path']}",
                        SOURCE_DRIFT,
                    )
                entries.append({
                    "delivery_id": delivery["delivery_id"],
                    "title": delivery["title"],
                    "revision": revision["revision"],
                    "reel_hash": revision["reel_hash"],
                    "imprint_id": revision["imprint_id"],
                    "role": item["role"],
                    "source_path": item["path"],
                    "sha256": item["sha256"],
                    "size": item["size"],
                })

    if not entries:
        raise DeliveryError("empty_selection", "Plan contains no deliverables")

    # Explicit exclusion record: prove private material is deliberately omitted.
    excluded = catalog.get("excluded_from_delivery", [])
    plan = {
        "schema": SCHEMA,
        "created_at": now_iso(),
        "source_root": str(source_root),
        "source_catalog": args.catalog.name,
        "delivery_ids": selected,
        "excluded_paths": excluded,
        "file_count": len(entries),
        "total_bytes": sum(e["size"] for e in entries),
        "files": sorted(entries, key=lambda e: (e["delivery_id"], e["revision"], e["role"])),
    }
    plan["plan_sha256"] = hashlib.sha256(canonical_json(
        {k: v for k, v in plan.items() if k != "plan_sha256"})).hexdigest()

    args.plan.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(args.plan, canonical_json(plan) + b"\n")
    print(f"plan frozen: {len(entries)} files, {plan['total_bytes']} bytes -> {args.plan}")
    print(f"deliveries: {', '.join(selected)}")
    if excluded:
        print(f"excluded (never copied): {len(excluded)} private path(s)")
    return OK


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# deliver
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class FileOutcome:
    source_path: str
    dest_path: str
    role: str
    sha256: str
    size: int
    status: str           # verified | retransmitted | source_drift | source_missing | failed
    detail: str = ""
    bytes_copied: int = 0

    def as_dict(self):
        return dataclasses.asdict(self)


class DeliverySession:
    def __init__(self, plan: dict, dest_root: Path, fault: Optional[dict],
                 revalidate: bool):
        self.plan = plan
        self.source_root = Path(plan["source_root"])
        self.dest_root = dest_root.resolve()
        self.fault = fault or {}
        self.revalidate = revalidate
        self.bytes_budget = self.fault.get("fail_after_bytes")
        self.interrupted = False
        self.outcomes: list[FileOutcome] = []

    # -- fault injection modelling a physically removed drive --------------- #
    def _write_chunk(self, handle, chunk: bytes):
        try:
            handle.write(chunk)
        except OSError as exc:
            raise VolumeUnplugged(f"destination write failed: {exc}") from exc
        if self.bytes_budget is not None:
            self.bytes_budget -= len(chunk)
            if self.bytes_budget <= 0:
                # Model the drive disappearing: subsequent writes would EIO.
                raise VolumeUnplugged(
                    "simulated unplug: write budget exhausted "
                    f"({self.fault.get('fail_after_bytes')} bytes)")

    def _dest_path(self, entry: dict) -> Path:
        # Keep the source's relative layout inside package/media/.
        return self.dest_root / "media" / entry["source_path"]

    # -- one planned file ---------------------------------------------------- #
    def process_entry(self, entry: dict) -> FileOutcome:
        source = self.source_root / entry["source_path"]
        dest = self._dest_path(entry)
        part = dest.with_name(dest.name + PART_SUFFIX)
        outcome = FileOutcome(
            source_path=entry["source_path"],
            dest_path=str(dest.relative_to(self.dest_root)),
            role=entry["role"], sha256=entry["sha256"], size=entry["size"],
            status="failed",
        )

        # 1) A fully present, hash-correct destination is reused as-is,
        #    unless --revalidate forces a fresh read.
        if dest.is_file() and not self.revalidate:
            if dest.stat().st_size == entry["size"] and sha256_file(dest) == entry["sha256"]:
                outcome.status = "verified"
                outcome.detail = "reused already-verified destination copy"
                return outcome
            # Wrong size/hash: treat as damage, fall through to re-copy.
            outcome.detail = "existing destination damaged; will retransmit"
        elif dest.is_file() and self.revalidate:
            if dest.stat().st_size == entry["size"] and sha256_file(dest) == entry["sha256"]:
                outcome.status = "verified"
                outcome.detail = "revalidated by full re-read"
                return outcome
            outcome.detail = "revalidation mismatch; will retransmit"

        # 2) Never trust a leftover .part from an interrupted run.
        if part.exists():
            part.unlink()

        # 3) Source must still exist AND still hash to the pinned value.
        if not source.is_file():
            outcome.status = "source_missing"
            outcome.detail = "pinned source no longer present; manual re-export required"
            return outcome
        if sha256_file(source) != entry["sha256"]:
            outcome.status = "source_drift"
            outcome.detail = ("source bytes changed after the plan was frozen; "
                              "new revision NOT silently adopted")
            return outcome

        # 4) Stream source -> .part, then hash and atomically rename.
        dest.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        try:
            with source.open("rb") as src, part.open("wb") as dst:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    self._write_chunk(dst, chunk)
                    copied += len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
        except VolumeUnplugged as exc:
            # Leave the .part in place; it will be discarded on the next run.
            self.interrupted = True
            outcome.status = "failed"
            outcome.bytes_copied = copied
            outcome.detail = f"interrupted: {exc}; partial file retained but not trusted"
            return outcome
        except OSError as exc:
            self.interrupted = True
            outcome.status = "failed"
            outcome.bytes_copied = copied
            outcome.detail = f"destination I/O error: {exc}"
            return outcome

        if part.stat().st_size != entry["size"] or sha256_file(part) != entry["sha256"]:
            outcome.status = "failed"
            outcome.bytes_copied = copied
            outcome.detail = "copied bytes failed end-to-end hash check"
            part.unlink()
            return outcome

        os.replace(part, dest)
        outcome.status = "retransmitted"
        outcome.bytes_copied = copied
        outcome.detail = "streamed from pinned source and hash-verified"
        return outcome

    def run(self) -> dict:
        for entry in self.plan["files"]:
            if self.interrupted:
                # Drive is gone: stop now, keep prior outcomes, do not touch
                # remaining files so a re-plugged run resumes deterministically.
                break
            self.outcomes.append(self.process_entry(entry))
        return self._summarize()

    def _summarize(self) -> dict:
        counts: dict[str, int] = {}
        for o in self.outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
        planned = len(self.plan["files"])
        processed = len(self.outcomes)
        acceptable = [o for o in self.outcomes if o.status in ("verified", "retransmitted")]
        blocked = [o for o in self.outcomes
                   if o.status in ("source_drift", "source_missing", "failed")]
        complete = (processed == planned and not blocked and not self.interrupted)
        return {
            "planned_files": planned,
            "processed_files": processed,
            "counts": counts,
            "interrupted": self.interrupted,
            "complete": complete,
            "acceptable": [o.as_dict() for o in acceptable],
            "blocked": [o.as_dict() for o in blocked],
        }


def _load_fault(path: Optional[Path]) -> Optional[dict]:
    if not path:
        return None
    fault = load_json(path)
    if "fail_after_bytes" in fault:
        fault["fail_after_bytes"] = int(fault["fail_after_bytes"])
    return fault


def cmd_deliver(args) -> int:
    plan = load_json(args.plan)
    if plan.get("schema") != SCHEMA:
        raise DeliveryError("bad_plan", "Unsupported delivery-plan schema")
    dest = args.destination.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    if not os.access(dest, os.W_OK):
        raise DeliveryError("dest_readonly", f"Destination not writable: {dest}")

    fault = _load_fault(args.fault_inject)
    session = DeliverySession(plan, dest, fault, args.revalidate)
    summary = session.run()

    # Write the machine-readable acceptance record (cumulative across runs).
    receipt_path = dest / RECEIPT_NAME
    history = []
    if receipt_path.exists():
        try:
            history = load_json(receipt_path).get("runs", [])
        except DeliveryError:
            history = []
    run_record = {
        "run_at": now_iso(),
        "plan_sha256": plan["plan_sha256"],
        "interrupted": summary["interrupted"],
        "complete": summary["complete"],
        "counts": summary["counts"],
        "outcomes": [o.as_dict() for o in session.outcomes],
    }
    if fault:
        run_record["fault_injection"] = fault
    history.append(run_record)

    # Package-level manifest only when every planned file is acceptable.
    overall_status = "incomplete"
    if summary["complete"]:
        overall_status = "complete"
        _write_package_manifest(plan, dest)
    elif any(o["status"] == "source_drift" for o in summary["blocked"]):
        overall_status = "blocked_source_drift"
    elif any(o["status"] == "source_missing" for o in summary["blocked"]):
        overall_status = "blocked_source_missing"
    elif summary["interrupted"]:
        overall_status = "interrupted_resumable"

    receipt = {
        "schema": SCHEMA,
        "package_status": overall_status,
        "plan_sha256": plan["plan_sha256"],
        "delivery_ids": plan["delivery_ids"],
        "excluded_paths": plan["excluded_paths"],
        "planned_files": summary["planned_files"],
        "verified_files": len(summary["acceptable"]),
        "blocked_files": len(summary["blocked"]),
        "last_run_at": run_record["run_at"],
        "runs": history,
        "legend": {
            "verified": "present on destination and full-hash matched the frozen plan",
            "retransmitted": "copied from the pinned source and hash-verified this run",
            "source_drift": "source changed after planning; new revision refused",
            "source_missing": "pinned source absent; requires explicit re-export",
            "failed": "copy interrupted or failed; partial file is never trusted",
        },
    }
    write_atomic(receipt_path, canonical_json(receipt) + b"\n")

    # Human-readable console summary.
    print(f"package status : {overall_status}")
    print(f"files verified : {len(summary['acceptable'])}/{summary['planned_files']}")
    for outcome in session.outcomes:
        marker = {"verified": "=", "retransmitted": "+", "source_drift": "!",
                  "source_missing": "!", "failed": "x"}.get(outcome.status, "?")
        print(f"  [{marker}] {outcome.status:16s} {outcome.source_path}")
    if summary["interrupted"]:
        print("volume interrupted: re-run after reconnecting; verified files are reused")
        return INTERRUPTED
    if any(o.status == "source_drift" for o in session.outcomes):
        return SOURCE_DRIFT
    if any(o.status == "source_missing" for o in session.outcomes):
        return SOURCE_DRIFT
    if summary["complete"]:
        return OK
    return CORRUPT


def _write_package_manifest(plan: dict, dest: Path) -> None:
    files = []
    for entry in plan["files"]:
        rel = Path("media") / entry["source_path"]
        target = dest / rel
        files.append({
            "delivery_id": entry["delivery_id"],
            "title": entry["title"],
            "revision": entry["revision"],
            "reel_hash": entry["reel_hash"],
            "imprint_id": entry["imprint_id"],
            "role": entry["role"],
            "path": str(rel),
            "sha256": entry["sha256"],
            "size": entry["size"],
        })
    manifest = {
        "schema": SCHEMA,
        "generated_at": now_iso(),
        "plan_sha256": plan["plan_sha256"],
        "delivery_ids": plan["delivery_ids"],
        "file_count": len(files),
        "total_bytes": sum(f["size"] for f in files),
        "files": sorted(files, key=lambda f: (f["delivery_id"], f["revision"], f["role"])),
    }
    write_atomic(dest / MANIFEST_NAME, canonical_json(manifest) + b"\n")

    # Ship the standalone verifier inside the package so the receiver does not
    # depend on the sender's toolchain.
    verifier_source = Path(__file__).with_name(VERIFIER_NAME)
    write_atomic(dest / VERIFIER_NAME, verifier_source.read_bytes())


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline resumable media delivery")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="freeze a pinned delivery plan")
    p_plan.add_argument("--catalog", required=True, type=Path)
    p_plan.add_argument("--plan", required=True, type=Path)
    p_plan.add_argument("--delivery", action="append",
                        help="delivery id to include (repeatable; default: all)")
    p_plan.set_defaults(func=cmd_plan)

    p_deliver = sub.add_parser("deliver", help="copy/verify a plan onto a volume")
    p_deliver.add_argument("--plan", required=True, type=Path)
    p_deliver.add_argument("--destination", required=True, type=Path)
    p_deliver.add_argument("--fault-inject", type=Path,
                           help="JSON file enabling controlled unplug simulation")
    p_deliver.add_argument("--revalidate", action="store_true",
                           help="force full re-read of every existing destination file")
    p_deliver.set_defaults(func=cmd_deliver)

    p_verify = sub.add_parser("verify", help="offline verification of a package")
    p_verify.add_argument("--package", required=True, type=Path)
    p_verify.add_argument("--expect-complete", action="store_true")
    p_verify.set_defaults(func=cmd_verify)
    return parser


def cmd_verify(args) -> int:
    # Delegate to the standalone verifier shipped in the package when present,
    # otherwise use the bundled copy next to this tool.
    bundled = Path(__file__).with_name(VERIFIER_NAME)
    shipped = args.package / VERIFIER_NAME
    verifier = shipped if shipped.exists() else bundled
    namespace: dict = {}
    exec(compile(verifier.read_text(encoding="utf-8"), str(verifier), "exec"),
         namespace)
    return namespace["verify_package"](args.package, args.expect_complete)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DeliveryError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
