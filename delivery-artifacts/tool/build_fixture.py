#!/usr/bin/env python3
"""Build a realistic Reel Loom production volume for the offline-delivery drill.

The script drives the *real* reel_loom service in-process (no production
service is touched; every byte lives under the drill temp directory):

  * several distinguishable MP4/WAV sources are encoded with FFmpeg,
  * multiple reels, each with an old and a new revision,
  * frozen renditions + caption tracks + ZIP imprints per revision,
  * non-deliverable material (question bank / private audit) that the
    delivery tool must never pick up.

It then exports a catalog of selected deliverables and their frozen files.

Usage:
  build_fixture.py build  --out DIR --workspace DIR
  build_fixture.py drift  --workspace DIR [--new-revision REEL_DIR ...]

The "drift" subcommand simulates the production service continuing to edit
after a delivery has been planned: it appends a fresh revision and re-renders.
"""
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import replace
from pathlib import Path

from reel_loom.config import Settings
from reel_loom.application import Application
from reel_loom.api import Api
from reel_loom.http import MediaHttpServer
from reel_loom.client import Client
import threading


# Per-reel distinguishable source material (ffmpeg lavfi).
SOURCE_SPECS = [
    {
        "slug": "harbor",
        "label": "harbor-waves",
        "video": "testsrc2=size=320x180:rate=25:duration=4",
        "audio": "sine=frequency=330:sample_rate=48000:duration=4",
        "title": "港湾潮声",
    },
    {
        "slug": "lantern",
        "label": "lantern-fair",
        "video": "smptebars=size=320x180:rate=25:duration=4",
        "audio": "sine=frequency=523:sample_rate=48000:duration=4",
        "title": "灯会长卷",
    },
    {
        "slug": "tidewatch",
        "label": "tide-watch",
        "video": "gradients=size=320x180:rate=25:duration=4:speed=0.05",
        "audio": "sine=frequency=784:sample_rate=48000:duration=4",
        "title": "观潮手记",
    },
    {
        "slug": "nightferry",
        "label": "night-ferry",
        "video": "testsrc=size=320x180:rate=25:duration=4",
        "audio": "sine=frequency=220:sample_rate=48000:duration=4",
        "title": "夜渡记录",
    },
]

SRT_TEMPLATE = """1
00:00:00,200 --> 00:00:01,800
{title}·第一句字幕

2
00:00:02,000 --> 00:00:03,600
{title}·第二句字幕
"""


class Harness:
    def __init__(self, workspace: Path):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        self.settings = Settings(
            root=workspace / "service-data", ffmpeg=ffmpeg, render_timeout=60, workers=1
        )
        self.app = Application(self.settings, start_workers=False)
        self.server = MediaHttpServer(("127.0.0.1", 0), Api(self.app))
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.client = Client(
            f"http://127.0.0.1:{self.server.server_port}/loom/v1", timeout=60
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.close()


def make_source(harness: Harness, spec: dict) -> bytes:
    app = harness.app
    with app.blobs.temporary(".mp4") as sample:
        app.processes.run(
            [
                "-f", "lavfi", "-i", spec["video"],
                "-f", "lavfi", "-i", spec["audio"],
                "-af", "volume=enable='between(t,1,1.4)':volume=0",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(sample),
            ]
        )
        return sample.read_bytes()


def build(out: Path, workspace: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    harness = Harness(workspace)
    catalog = {"schema": 1, "producer": "reel-loom-drill", "deliveries": []}
    try:
        client, app = harness.client, harness.app
        # Two frozen look specs: video and audio.
        client.put_look("preview", {"kind": "video", "width": 320, "height": 180})
        client.put_look("listening", {"kind": "audio", "sample_rate": 48000})

        for index, spec in enumerate(SOURCE_SPECS, start=1):
            content = make_source(harness, spec)
            asset = client.ingest(content, spec["label"])
            # Caption source track (real SRT ingested by the service).
            srt_body = SRT_TEMPLATE.format(title=spec["title"])
            track = client.request(
                "POST", f"/assets/{asset['id']}/cues",
                {"language": "zh-CN", "srt": srt_body},
            )
            slices_v1 = [
                {"asset": asset["id"], "in": 0.2, "out": 1.6, "label": f"{spec['title']}·开场"},
                {"asset": asset["id"], "in": 2.0, "out": 3.6, "label": f"{spec['title']}·收束"},
            ]
            reel = client.create_reel(spec["title"], slices_v1, key=f"reel-{spec['slug']}")
            # Revision 2 (new edit): reversed order / different window.
            slices_v2 = [
                {"asset": asset["id"], "in": 1.0, "out": 2.4, "label": f"{spec['title']}·新稿前段"},
                {"asset": asset["id"], "in": 0.2, "out": 1.2, "label": f"{spec['title']}·新稿后段"},
            ]
            updated = client.replace_reel(reel["id"], 1, spec["title"] + "·修订二", slices_v2)
            assert updated["revision"] == 2

            delivery = {
                "delivery_id": f"DL-{index:02d}-{spec['slug']}",
                "title": spec["title"],
                "reel_id": reel["id"],
                "asset_id": asset["id"],
                "cue_track_id": track["id"],
                "revisions": [],
            }

            for revision in (1, 2):
                # Video rendition for the frozen revision.
                ticket_v = client.render(
                    reel["id"], "preview", revision=revision,
                    key=f"{spec['slug']}-v-r{revision}",
                )
                ticket_a = client.render(
                    reel["id"], "listening", revision=revision,
                    key=f"{spec['slug']}-a-r{revision}",
                )
                while app.renderer.run_once() is not None:
                    pass
                ticket_v = client.wait(ticket_v["id"], timeout=60)
                ticket_a = client.wait(ticket_a["id"], timeout=60)
                assert ticket_v["state"] == "succeeded" and ticket_a["state"] == "succeeded"

                # Imprint (ZIP) freezes the rendition + projected captions.
                imprint = client.imprint(ticket_v["id"], cue_tracks=[track["id"]])
                verify = client.request("GET", f"/imprints/{imprint['id']}/verify")
                assert verify["valid"]

                # Projected caption SRT for this exact revision.
                projected = client.request(
                    "POST", f"/reels/{reel['id']}/captions",
                    {"revision": revision, "tracks": [track["id"]]},
                )

                rev_dir = out / delivery["delivery_id"] / f"revision-{revision}"
                media_dir = rev_dir / "media"
                media_dir.mkdir(parents=True, exist_ok=True)

                def export_artifact(ticket: dict, suffix: str) -> dict:
                    artifact = ticket["artifacts"][0]
                    blob = app.blobs.get(artifact["blob"])
                    target = media_dir / f"{artifact['id']}.{suffix}"
                    shutil.copyfile(app.blobs.resolve(artifact["blob"]), target)
                    return {
                        "role": "video_rendition" if suffix == "mp4" else "audio_rendition",
                        "path": str(target.relative_to(out)),
                        "sha256": artifact["blob"],
                        "size": blob["size"],
                        "artifact_id": artifact["id"],
                        "ticket_id": ticket["id"],
                    }

                files = [
                    export_artifact(ticket_v, "mp4"),
                    export_artifact(ticket_a, "wav"),
                ]

                # Frozen caption projection (standalone SRT, offline-readable).
                caption_path = rev_dir / "captions.zh-CN.srt"
                caption_path.write_text(projected["srt"], encoding="utf-8")
                files.append(
                    {
                        "role": "frozen_captions",
                        "path": str(caption_path.relative_to(out)),
                        "sha256": _sha256(caption_path),
                        "size": caption_path.stat().st_size,
                        "language": "zh-CN",
                        "reel_hash": projected["reel_hash"],
                    }
                )

                # Frozen manuscript: human-readable freeze record of this
                # revision's edit document + render snapshot.
                manuscript = _build_manuscript(spec, revision, reel, ticket_v, ticket_a, projected)
                manuscript_path = rev_dir / "frozen-manuscript.md"
                manuscript_path.write_text(manuscript, encoding="utf-8")
                files.append(
                    {
                        "role": "frozen_manuscript",
                        "path": str(manuscript_path.relative_to(out)),
                        "sha256": _sha256(manuscript_path),
                        "size": manuscript_path.stat().st_size,
                    }
                )

                # The service's own frozen imprint ZIP (deterministic archive).
                imprint_path = rev_dir / "service-imprint.zip"
                shutil.copyfile(app.blobs.resolve(imprint["blob"]), imprint_path)
                files.append(
                    {
                        "role": "service_imprint_zip",
                        "path": str(imprint_path.relative_to(out)),
                        "sha256": imprint["blob"],
                        "size": imprint_path.stat().st_size,
                        "imprint_id": imprint["id"],
                        "imprint_fingerprint": imprint["fingerprint"],
                    }
                )

                delivery["revisions"].append(
                    {
                        "revision": revision,
                        "reel_hash": _reel_hash(client, reel["id"], revision),
                        "video_ticket": ticket_v["id"],
                        "audio_ticket": ticket_a["id"],
                        "imprint_id": imprint["id"],
                        "files": files,
                    }
                )
            catalog["deliveries"].append(delivery)

        # Non-deliverable material that must never enter a customer package.
        private = out / "_PRIVATE_DO_NOT_DELIVER"
        private.mkdir(exist_ok=True)
        qb = private / "question-bank.md"
        qb.write_text("# 内部题库（机密）\n\n1. 验收暗桩题答案……\n", encoding="utf-8")
        audit = private / "audit-notes.md"
        audit.write_text("# 私人审计材料\n\n内部难度证据，禁止外发。\n", encoding="utf-8")
        catalog["excluded_from_delivery"] = [
            str(qb.relative_to(out)),
            str(audit.relative_to(out)),
        ]

        (out / "source-catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    finally:
        harness.close()


def _reel_hash(client: Client, reel_id: str, revision: int) -> str:
    return client.request("GET", f"/reels/{reel_id}?revision={revision}")["hash"]


def _sha256(path: Path) -> str:
    import hashlib

    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _build_manuscript(spec, revision, reel, ticket_v, ticket_a, projected) -> str:
    lines = [
        f"# 冻结稿件 · {spec['title']} · 修订 {revision}",
        "",
        f"- 剪辑卷: `{reel['id']}`",
        f"- 修订号: {revision}",
        f"- 视频渲染单: `{ticket_v['id']}`（状态 {ticket_v['state']}）",
        f"- 音频渲染单: `{ticket_a['id']}`（状态 {ticket_a['state']}）",
        f"- 视频快照指纹: `{ticket_v['fingerprint']}`",
        f"- 音频快照指纹: `{ticket_a['fingerprint']}`",
        f"- 字幕投影条目数: {len(projected['cues'])}",
        "",
        "## 冻结的剪辑文档",
        "",
        "```json",
        json.dumps(ticket_v["snapshot"]["document"], ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "## 冻结的版式",
        "",
        "```json",
        json.dumps(ticket_v["snapshot"]["look"], ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "本稿件与同目录媒体、字幕及服务印本的摘要共同写入交付清单，",
        "接收者无需数据库即可按清单逐一核对字节。",
        "",
    ]
    return "\n".join(lines)


def drift(workspace: Path, out: Path) -> None:
    """Simulate continued production after planning: add revision 3 to reel #1."""
    harness = Harness(workspace)
    catalog = json.loads((out / "source-catalog.json").read_text(encoding="utf-8"))
    try:
        client, app = harness.client, harness.app
        target = catalog["deliveries"][0]  # harbor reel
        reel_id = target["reel_id"]
        asset_id = target["asset_id"]
        head = client.request("GET", f"/reels/{reel_id}")
        assert head["revision"] == 2, "unexpected head before drift"
        slices_v3 = [
            {"asset": asset_id, "in": 0.4, "out": 3.4, "label": "制作期间新增的第三稿"},
        ]
        new = client.replace_reel(reel_id, 2, head["title"] + "·修订三", slices_v3)
        ticket = client.render(reel_id, "preview", revision=3, key="harbor-drift-r3")
        while app.renderer.run_once() is not None:
            pass
        ticket = client.wait(ticket["id"], timeout=60)
        assert ticket["state"] == "succeeded"
        # Record the drift in the live catalog without rewriting frozen entries.
        target["post_plan_head_revision"] = new["revision"]
        target["post_plan_reel_hash"] = new["hash"]
        target["post_plan_ticket"] = ticket["id"]
        (out / "source-catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(json.dumps({"drift": "revision-3-created",
                          "reel": reel_id, "revision": new["revision"],
                          "hash": new["hash"]}, ensure_ascii=False))
    finally:
        harness.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build")
    p_build.add_argument("--out", required=True, type=Path)
    p_build.add_argument("--workspace", required=True, type=Path)
    p_drift = sub.add_parser("drift")
    p_drift.add_argument("--out", required=True, type=Path)
    p_drift.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    if args.cmd == "build":
        build(args.out, args.workspace)
        print("fixture built at", args.out)
    else:
        drift(args.workspace, args.out)


if __name__ == "__main__":
    main()
