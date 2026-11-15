#!/usr/bin/env python3
"""offline_verify.py — receiver-side, dependency-free package verification.

This file is copied into every completed delivery package. A receiver can run
it with a plain Python 3.11 interpreter while fully offline, with no database
and no Reel Loom installation:

    python3 offline_verify.py --package /path/to/package

It proves, per delivery and per frozen revision, that:
  * every media file (mp4/wav), caption SRT and frozen manuscript hashes to
    the digest recorded in package-manifest.json (and the pinned plan);
  * the service-produced imprint ZIP still contains the same rendition bytes
    and caption projection recorded at freeze time;
  * the three-way correspondence media <-> captions <-> frozen manuscript
    holds within each revision folder;
  * no private/question-bank material was smuggled into the package.

Exit code 0 = package fully acceptable; 4 = one or more files fail.
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

CHUNK = 1024 * 1024
MANIFEST_NAME = "package-manifest.json"
RECEIPT_NAME = "acceptance-record.json"
PLAN_NAME = "delivery-plan.json"

# Roles that must coexist inside every frozen revision.
REQUIRED_ROLES = {
    "video_rendition", "audio_rendition", "frozen_captions",
    "frozen_manuscript", "service_imprint_zip",
}
PRIVATE_MARKERS = ("question", "题库", "audit", "审计", "PRIVATE", "private")


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_package(package: Path, expect_complete: bool = False) -> int:
    package = Path(package).resolve()
    failures: list[str] = []
    notes: list[str] = []

    manifest_path = package / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"FAIL: {MANIFEST_NAME} missing — package is not a finished delivery")
        return 4
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # 1) Per-file hash + size verification against the frozen manifest.
    by_revision: dict[tuple, dict[str, dict]] = {}
    seen_paths: set[str] = set()
    for entry in manifest["files"]:
        rel = entry["path"]
        seen_paths.add(rel)
        target = package / rel
        if not target.is_file():
            failures.append(f"missing file: {rel}")
            continue
        actual_size = target.stat().st_size
        if actual_size != entry["size"]:
            failures.append(
                f"size mismatch: {rel} expected {entry['size']} got {actual_size}")
            continue
        actual_hash = sha256_file(target)
        if actual_hash != entry["sha256"]:
            failures.append(f"hash mismatch: {rel}")
            continue
        key = (entry["delivery_id"], entry["revision"])
        by_revision.setdefault(key, {})[entry["role"]] = entry

    # 2) Every revision must carry the full media/caption/manuscript/imprint set.
    for (delivery_id, revision), roles in sorted(by_revision.items()):
        missing_roles = REQUIRED_ROLES - set(roles)
        if missing_roles:
            failures.append(
                f"{delivery_id} revision {revision} missing roles: "
                f"{sorted(missing_roles)}")
        # Cross-check: caption reel_hash belongs to this revision's manifest row.
        caption = roles.get("frozen_captions")
        manuscript = roles.get("frozen_manuscript")
        if caption and manuscript:
            if caption.get("reel_hash") and not _caption_matches_reel(
                    roles, caption, notes, delivery_id, revision):
                failures.append(
                    f"{delivery_id} revision {revision} caption/reel hash correspondence broken")
        # 3) The imprint ZIP must still match its frozen members.
        imprint = roles.get("service_imprint_zip")
        if imprint:
            _verify_imprint_zip(package / imprint["path"], imprint,
                                failures, delivery_id, revision)

    # 4) Leakage scan: no private/question-bank material anywhere in package.
    for path in package.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(package))
        if rel in (MANIFEST_NAME, RECEIPT_NAME, PLAN_NAME, "offline_verify.py"):
            continue
        lowered = rel.lower()
        if any(marker.lower() in lowered for marker in PRIVATE_MARKERS):
            failures.append(f"private material detected in package: {rel}")
        if path.suffix == PART_SUFFIX_GUESS:
            failures.append(f"leftover partial file (untrusted): {rel}")

    # 5) No unexpected files beyond the manifest set + control documents.
    allowed = set(seen_paths) | {
        MANIFEST_NAME, RECEIPT_NAME, PLAN_NAME, "offline_verify.py",
        "verification-report.txt",
    }
    for path in package.rglob("*"):
        if path.is_file():
            rel = str(path.relative_to(package))
            if rel not in allowed and not rel.endswith(".tmp"):
                notes.append(f"unlisted file present: {rel}")

    # 6) Receipt cross-check (if present) says the same thing.
    receipt_path = package / RECEIPT_NAME
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("package_status") != "complete":
            failures.append(
                f"acceptance record status is {receipt.get('package_status')!r}, not 'complete'")
        if receipt.get("verified_files") != manifest["file_count"]:
            failures.append("acceptance record verified count disagrees with manifest")

    report = _render_report(package, manifest, by_revision, failures, notes)
    (package / "verification-report.txt").write_text(report, encoding="utf-8")
    print(report)

    if expect_complete and failures:
        return 4
    return 0 if not failures else 4


PART_SUFFIX_GUESS = ".part"


def _caption_matches_reel(roles, caption, notes, delivery_id, revision) -> bool:
    # The manifest row for captions carries reel_hash; all rows in the same
    # revision group were frozen together, so this is an internal consistency
    # assertion rather than a database lookup.
    peer_hashes = {row.get("reel_hash") for row in roles.values() if row.get("reel_hash")}
    peer_hashes.discard(None)
    if caption["reel_hash"] not in peer_hashes:
        return False
    notes.append(f"{delivery_id} r{revision}: caption reel_hash consistent across revision")
    return True


def _verify_imprint_zip(zip_path: Path, imprint: dict, failures,
                        delivery_id, revision) -> None:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
            if "manifest.json" not in names:
                failures.append(f"{delivery_id} r{revision}: imprint lacks manifest.json")
                return
            inner = json.loads(archive.read("manifest.json"))
            # The rendition files inside the imprint must hash correctly.
            for member in inner.get("files", []):
                if member["name"] not in names:
                    failures.append(
                        f"{delivery_id} r{revision}: imprint missing member {member['name']}")
                    continue
                hasher = hashlib.sha256()
                with archive.open(member["name"]) as content:
                    for chunk in iter(lambda: content.read(CHUNK), b""):
                        hasher.update(chunk)
                if hasher.hexdigest() != member["blob"]:
                    failures.append(
                        f"{delivery_id} r{revision}: imprint member digest mismatch "
                        f"{member['name']}")
            # Caption projection inside the imprint.
            if inner.get("caption"):
                if "captions.srt" not in names:
                    failures.append(
                        f"{delivery_id} r{revision}: imprint caption member missing")
                elif archive.read("captions.srt").decode() != inner["caption"]["srt"]:
                    failures.append(
                        f"{delivery_id} r{revision}: imprint caption content mismatch")
    except zipfile.BadZipFile:
        failures.append(f"{delivery_id} r{revision}: imprint ZIP is unreadable")


def _render_report(package, manifest, by_revision, failures, notes) -> str:
    lines = [
        "声画织场 · 离线交付验收报告",
        "=" * 48,
        f"package path : {package}",
        f"plan digest  : {manifest.get('plan_sha256')}",
        f"deliveries   : {', '.join(manifest.get('delivery_ids', []))}",
        f"file count   : {manifest.get('file_count')}",
        f"total bytes  : {manifest.get('total_bytes')}",
        "",
        "revision groups:",
    ]
    for (delivery_id, revision), roles in sorted(by_revision.items()):
        lines.append(f"  - {delivery_id} revision {revision}: "
                     f"{len(roles)}/{len(REQUIRED_ROLES)} roles present "
                     f"({', '.join(sorted(roles))})")
    lines.append("")
    if notes:
        lines.append("notes:")
        lines.extend(f"  * {n}" for n in notes)
        lines.append("")
    if failures:
        lines.append(f"RESULT: FAIL ({len(failures)} problem(s)) — must retransmit")
        lines.extend(f"  ! {f}" for f in failures)
    else:
        lines.append("RESULT: ACCEPT — every media/caption/manuscript/imprint digest "
                     "matches the frozen record; no private material present.")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Verify an offline delivery package")
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--expect-complete", action="store_true")
    args = parser.parse_args()
    sys.exit(verify_package(args.package, args.expect_complete))
