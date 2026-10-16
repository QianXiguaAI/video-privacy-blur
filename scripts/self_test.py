#!/usr/bin/env python3
"""Behavioral tests plus optional local Vision/FFmpeg end-to-end fixtures."""

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys
import zipfile
from unittest.mock import patch
from types import SimpleNamespace

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "video_privacy", HERE / "video_privacy.py"
)
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)


class BehaviorTests(unittest.TestCase):
    def test_static_ocr_jitter_locks_position_and_size(self):
        data = {"width": 1284, "height": 2778, "pts": [i / 30 for i in range(60)]}
        offsets = [0, 2, -1, 3, -2]
        detections = [
            [
                {
                    "type": "email",
                    "identity": "field",
                    "box": [
                        100 + offsets[i % 5],
                        200 + offsets[(i + 1) % 5],
                        500 + offsets[i % 5],
                        240 + offsets[(i + 1) % 5],
                    ],
                }
            ]
            for i in range(60)
        ]
        raw, stable, layers, stats = v.plan_regions(
            data, detections, copy.deepcopy(v.DEFAULT_POLICY), []
        )
        self.assertGreater(len({tuple(x[0]) for x in raw}), 1)
        self.assertEqual(len({tuple(x[0]) for x in stable}), 1)
        self.assertGreater(stats["raw_locked_edge_change"], 0)
        self.assertEqual(stats["output_locked_edge_change"], 0)
        self.assertTrue(all(v.contains(b[0], a[0]) for a, b in zip(raw, stable)))

    def test_static_height_variation_keeps_one_legacy_track(self):
        data = {"width": 1284, "height": 2778, "pts": [i / 30 for i in range(60)]}
        detections = [
            [
                {
                    "type": "email",
                    "box": [
                        100 + [0, 4, -3][i % 3],
                        200 + [0, 16, 4][i % 3],
                        700 + [0, 3, 1][i % 3],
                        260 + [-5, -2, 2][i % 3],
                    ],
                }
            ]
            for i in range(60)
        ]
        raw, stable, _, stats = v.plan_regions(
            data, detections, copy.deepcopy(v.DEFAULT_POLICY), []
        )
        self.assertEqual(stats["tracks"], 1)
        self.assertEqual(len({tuple(boxes[0]) for boxes in stable}), 1)
        self.assertTrue(all(v.contains(b[0], a[0]) for a, b in zip(raw, stable)))

    def test_nearby_instances_keep_tracks_when_input_order_changes(self):
        data = {"width": 480, "height": 360, "pts": [i / 30 for i in range(30)]}
        detections = []
        for i in range(30):
            fields = [
                {
                    "type": "email",
                    "identity": "same",
                    "box": [20 + i % 3, 40, 200 + i % 3, 60],
                },
                {
                    "type": "email",
                    "identity": "same",
                    "box": [20 - i % 3, 95, 200 - i % 3, 115],
                },
            ]
            detections.append(fields if i % 2 else list(reversed(fields)))
        _, stable = v.make_regions(
            data, detections, copy.deepcopy(v.DEFAULT_POLICY), []
        )
        self.assertTrue(all(len(boxes) == 2 for boxes in stable))
        self.assertEqual(len({tuple(map(tuple, boxes)) for boxes in stable}), 1)

    def test_slow_scroll_moves_without_long_stationary_steps(self):
        data = {"width": 480, "height": 800, "pts": [i / 30 for i in range(90)]}
        detections = [
            [
                {
                    "type": "email",
                    "identity": "slow",
                    "box": [50, 100 + i * 0.5, 250, 125 + i * 0.5],
                }
            ]
            for i in range(90)
        ]
        raw, stable = v.make_regions(
            data, detections, copy.deepcopy(v.DEFAULT_POLICY), []
        )
        self.assertTrue(all(v.contains(b[0], a[0]) for a, b in zip(raw, stable)))
        tops = [f[0][1] for f in stable]
        longest = run = 1
        for a, b in zip(tops, tops[1:]):
            run = run + 1 if a == b else 1
            longest = max(longest, run)
        self.assertLessEqual(longest, 4)
        self.assertLessEqual(
            max(
                abs((b[0][1] + b[0][3] - a[0][1] - a[0][3]) / 2)
                for a, b in zip(raw, stable)
            ),
            5,
        )

    def test_payment_barcode_needs_context_and_includes_printed_number(self):
        barcode = {"box": [20, 80, 300, 140], "symbology": "Code128", "confidence": 1}
        frame = {
            "texts": [
                {"text": "付款碼", "box": [20, 20, 100, 40], "tokens": []},
                {
                    "text": "123456789012345678",
                    "box": [20, 150, 300, 175],
                    "tokens": [],
                },
            ],
            "codes": [barcode],
        }
        strong = v.detect_frame(frame, v.DEFAULT_POLICY, {})
        self.assertEqual(len([h for h in strong if h["type"] == "payment_code"]), 2)
        ordinary = {
            "texts": [{"text": "商品", "box": [20, 20, 100, 40], "tokens": []}],
            "codes": [barcode],
        }
        hits = v.detect_frame(ordinary, v.DEFAULT_POLICY, {})
        self.assertFalse(any(h["type"] == "payment_code" for h in hits))
        self.assertEqual(
            v.review_candidates(ordinary, {}, hits)[0]["reason"], "weak-barcode-purpose"
        )

    def test_traditional_name_label_and_known_unlabelled_name(self):
        frame = {
            "texts": [
                {"text": "被保險人：陳小明", "box": [10, 10, 200, 30], "tokens": []}
            ],
            "codes": [],
        }
        self.assertTrue(
            any(
                h["type"] == "name" for h in v.detect_frame(frame, v.DEFAULT_POLICY, {})
            )
        )
        frame["texts"][0]["text"] = "陳小明"
        self.assertFalse(v.detect_frame(frame, v.DEFAULT_POLICY, {}))
        self.assertTrue(
            any(
                h["type"] == "name"
                for h in v.detect_frame(frame, v.DEFAULT_POLICY, {"name": {"陳小明"}})
            )
        )

    def test_solid_and_gaussian_modes_render_with_shared_coverage(self):
        data = {"width": 96, "height": 80, "pts": [0, 0.1]}
        detections = [
            [
                {"type": "email", "box": [8, 8, 35, 25]},
                {"type": "verification", "box": [55, 45, 85, 65]},
            ]
            for _ in data["pts"]
        ]
        policy = copy.deepcopy(v.DEFAULT_POLICY)
        policy["type_modes"] = {"verification": "solid"}
        raw, stable, layers, _ = v.plan_regions(data, detections, policy, [])
        self.assertTrue(all(layers["gaussian"]) and all(layers["solid"]))
        with tempfile.TemporaryDirectory(prefix="privacy-modes-") as folder:
            root = Path(folder)
            graph = v.graph(
                data,
                {"regions": stable, "layers": layers, "policy": policy},
                root / "cmd.txt",
            )
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=white:s=96x80:r=10:d=0.2",
                    "-filter_complex",
                    graph,
                    "-map",
                    "[v]",
                    "-pix_fmt",
                    "gray",
                    "-f",
                    "rawvideo",
                    "pipe:1",
                    "-map",
                    "[audit]",
                    "-f",
                    "null",
                    "-",
                ],
                cwd=root,
                capture_output=True,
                check=True,
            )
            self.assertEqual(len(result.stdout), 2 * 96 * 80)
            for i in range(2):
                self.assertLess(
                    max(
                        result.stdout[i * 96 * 80 + y * 96 + x]
                        for y in range(49, 61)
                        for x in range(60, 80)
                    ),
                    5,
                )
                self.assertGreater(result.stdout[i * 96 * 80 + 16 * 96 + 20], 240)

    def test_resume_reuses_only_valid_complete_chunks(self):
        with tempfile.TemporaryDirectory(prefix="privacy-resume-") as folder:
            root = Path(folder)
            source = root / "input.bin"
            source.write_bytes(b"synthetic")
            destination = root / "cache/scan.jsonl"
            data = {
                "width": 80,
                "height": 80,
                "pts": [0.023 + i * 0.04 for i in range(8)],
                "duration": 0.35,
            }
            calls = []
            interrupt = [True]

            def execute(args, *unused, **kwargs):
                begin, end = float(args[4]), float(args[5])
                calls.append(begin)
                if len(calls) == 2 and interrupt[0]:
                    raise RuntimeError("simulated interruption")
                rows = [
                    {
                        "frame": i,
                        "pts": t,
                        "width": 80,
                        "height": 80,
                        "texts": [],
                        "codes": [],
                    }
                    for i, t in enumerate(data["pts"])
                    if begin <= t < end
                ]
                Path(args[2]).write_text(
                    "".join(json.dumps(row) + "\n" for row in rows)
                )

            with patch.object(v, "helper", return_value="fake-vision"), patch.object(
                v, "run", side_effect=execute
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated"):
                    v.resumable_scan(
                        root, str(source), destination, data, 0, ["en-US"], 0.12
                    )
                self.assertFalse(destination.exists())
                interrupt[0] = False
                calls.clear()
                v.resumable_scan(
                    root, str(source), destination, data, 0, ["en-US"], 0.12
                )
                self.assertEqual(len(list(v.records(destination))), 8)
                self.assertTrue(
                    all(t > 0.1 for t in calls),
                    "The first completed chunk must not be decoded again",
                )
                parts = sorted((root / "cache").glob("scan-chunks-*/*.jsonl"))
                parts[0].write_text("corrupt")
                calls.clear()
                v.resumable_scan(
                    root, str(source), destination, data, 0, ["en-US"], 0.12
                )
                self.assertEqual(len(calls), 1)
                calls.clear()
                v.resumable_scan(
                    root, str(source), destination, data, 0, ["zh-Hant", "en-US"], 0.12
                )
                self.assertGreater(
                    len(calls), 1, "Language changes must invalidate prior checkpoints"
                )

    def test_package_excludes_bytecode_media_and_local_artifacts(self):
        spec = importlib.util.spec_from_file_location(
            "package_skill", HERE / "package_skill.py"
        )
        pack = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pack)
        with tempfile.TemporaryDirectory(prefix="privacy-package-") as folder:
            root = Path(folder) / "skill"
            (root / "scripts/__pycache__").mkdir(parents=True)
            for name in [
                "SKILL.md",
                "scripts/tool.py",
                "scripts/__pycache__/tool.pyc",
                "scripts/private.mp4",
                "notes/private.md",
            ]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")
            output = pack.package(root, Path(folder) / "skill.zip")
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(
                    set(archive.namelist()), {"skill/SKILL.md", "skill/scripts/tool.py"}
                )

    def test_timestamp_and_half_open_overrides(self):
        self.assertEqual(v.timestamp("`01:3`0"), 90)
        self.assertEqual(v.timestamp("2：19"), 139)
        rules = [
            {"start": 0, "end": 29, "action": "set", "types": []},
            {"start": 20, "end": 40, "action": "keep", "types": ["email"]},
        ]
        self.assertEqual(v.effective(v.DEFAULT_POLICY, rules, 19.9), set())
        self.assertEqual(v.effective(v.DEFAULT_POLICY, rules, 20), {"email"})
        self.assertEqual(
            v.effective(v.DEFAULT_POLICY, rules, 40), set(v.DEFAULT_POLICY["types"])
        )
        with self.assertRaises(RuntimeError):
            v.interval("2:00", "1:00", 200)

    def test_field_geometry_utf16_and_partial_input(self):
        text = "🍉 Contact demo.user@example.com, next"
        start = text.index("demo")
        end = text.index(",")
        row = {
            "text": text,
            "confidence": 0.99,
            "box": [0, 0, 400, 40],
            "tokens": [
                {
                    "start": v.utf16(text, start),
                    "end": v.utf16(text, end),
                    "box": [100, 5, 300, 35],
                }
            ],
        }
        hits = v.detect_frame({"texts": [row], "codes": []}, v.DEFAULT_POLICY, {})
        self.assertEqual(
            next(h["box"] for h in hits if h["type"] == "email"), [100, 5, 300, 35]
        )
        partial = {
            "texts": [
                {
                    "text": "demo.us",
                    "box": [10, 10, 100, 30],
                    "confidence": 0.9,
                    "tokens": [],
                }
            ],
            "codes": [],
        }
        self.assertTrue(
            any(
                x["type"] == "email"
                for x in v.detect_frame(
                    partial, v.DEFAULT_POLICY, {"email": {"demo.user@example.com"}}
                )
            )
        )

    def test_smoothing_covers_and_does_not_cross_clear_boundary(self):
        data = {"width": 480, "height": 360, "pts": [i / 24 for i in range(48)]}
        boxes = [
            [{"type": "email", "box": [50, 60 + i * 2, 250, 90 + i * 2]}]
            for i in range(48)
        ]
        rules = [{"start": 1, "end": 2, "action": "set", "types": []}]
        raw, smooth = v.make_regions(
            data, boxes, copy.deepcopy(v.DEFAULT_POLICY), rules
        )
        self.assertTrue(
            all(
                any(v.contains(a, b) for a in cov)
                for req, cov in zip(raw, smooth)
                for b in req
            )
        )
        self.assertTrue(all(not f for f in smooth[24:]))

    def test_cut_has_no_path_across_screen(self):
        data = {"width": 480, "height": 800, "pts": [i / 30 for i in range(12)]}
        det = [
            [
                {
                    "type": "email",
                    "box": [30, 100 if i < 6 else 600, 250, 130 if i < 6 else 630],
                }
            ]
            for i in range(12)
        ]
        raw, smooth = v.make_regions(data, det, copy.deepcopy(v.DEFAULT_POLICY), [])
        self.assertTrue(all(f[0][3] < 200 for f in smooth[:6]))
        self.assertTrue(all(f[0][1] > 500 for f in smooth[6:]))

    def test_confidence_and_non_sensitive_year(self):
        frame = {
            "texts": [
                {"text": "2026", "box": [0, 0, 30, 20], "confidence": 0.9, "tokens": []}
            ],
            "codes": [],
        }
        self.assertFalse(v.detect_frame(frame, v.DEFAULT_POLICY, {}))

    def test_email_ocr_variant_and_category_gap(self):
        row = {
            "text": "demo.userCexample.com",
            "box": [20, 40, 260, 65],
            "confidence": 0.7,
            "tokens": [],
        }
        hits = v.detect_frame(
            {"texts": [row], "codes": []},
            v.DEFAULT_POLICY,
            {"email": {"demo.user@example.com"}},
        )
        self.assertTrue(any(h["type"] == "email" for h in hits))
        data = {"width": 480, "height": 360, "pts": [0, 0.04, 0.08]}
        phone = {"type": "phone", "box": [20, 100, 200, 130]}
        email = {"type": "email", "box": [20, 40, 260, 65]}
        raw, _ = v.make_regions(
            data,
            [[phone, email], [phone], [phone, email]],
            copy.deepcopy(v.DEFAULT_POLICY),
            [],
        )
        self.assertTrue(any(v.contains(b, email["box"]) for b in raw[1]))

    def test_two_email_instances_do_not_hide_each_others_gap(self):
        data = {"width": 480, "height": 800, "pts": [0, 0.04, 0.08]}
        a = {"type": "email", "identity": "same-value", "box": [20, 50, 200, 75]}
        b = {"type": "email", "identity": "same-value", "box": [20, 250, 200, 275]}
        moved = dict(b, box=[20, 270, 200, 295])
        raw, _ = v.make_regions(
            data, [[a, b], [a], [a, moved]], copy.deepcopy(v.DEFAULT_POLICY), []
        )
        self.assertTrue(any(v.contains(box, [20, 260, 200, 285]) for box in raw[1]))
        self.assertEqual(len(raw[1]), 2)
        # No many-to-many interpolation creates extra bars when both remain visible.
        raw, _ = v.make_regions(
            data, [[a, b], [a, b], [a, b]], copy.deepcopy(v.DEFAULT_POLICY), []
        )
        self.assertEqual([len(boxes) for boxes in raw], [2, 2, 2])

    def test_gap_does_not_cross_identity_cut_rule_boundary_or_long_pause(self):
        data = {"width": 480, "height": 800, "pts": [0, 0.04, 0.08]}
        a = {"type": "email", "identity": "first", "box": [20, 100, 200, 125]}
        cases = [
            (data, dict(a, identity="different"), []),
            (data, dict(a, box=[20, 600, 200, 625]), []),
            (dict(data, pts=[0, 0.2, 0.4]), a, []),
            (data, a, [{"start": 0.01, "end": 0.03, "action": "set", "types": []}]),
        ]
        for timeline, last, rules in cases:
            with self.subTest(last=last, rules=rules, times=timeline["pts"]):
                raw, _ = v.make_regions(
                    timeline, [[a], [], [last]], copy.deepcopy(v.DEFAULT_POLICY), rules
                )
                self.assertFalse(raw[1])

    def test_weak_email_is_reviewed_even_when_another_email_is_detected(self):
        known = "demo.user@example.com"
        corrupt = "dem0.uslrCexamp1e.com"

        def row(text, box):
            return {"text": text, "box": box, "tokens": [], "confidence": 0.5}

        data = {"width": 480, "height": 360, "pts": [0, 0.04]}
        scan = [
            {
                "frame": i,
                "pts": t,
                "width": 480,
                "height": 360,
                "codes": [],
                "texts": [
                    row(known, [20, 40, 260, 65]),
                    row(corrupt, [20, 140, 260, 165]),
                ],
            }
            for i, t in enumerate(data["pts"])
        ]
        policy = copy.deepcopy(v.DEFAULT_POLICY)
        policy["types"] = ["email"]
        hints = {"email": {known}}
        strong = v.detect_frame(scan[0], policy, hints)
        self.assertFalse(any(v.contains(h["box"], [20, 140, 260, 165]) for h in strong))
        self.assertEqual(len(v.weak_email_candidates(scan[0], hints, strong)), 1)
        masks = [[[10, 30, 270, 75]], [[10, 30, 270, 75]]]
        findings = v.audit_source_frames(data, scan, policy, [], masks)
        self.assertEqual([f["frame"] for f in findings], [0, 1])
        policy["manual_regions"] = [
            {"start": 0, "end": 0.08, "type": "email", "box": [10, 130, 270, 175]}
        ]
        detections = [v.detect_frame(f, policy, hints) for f in scan]
        raw, _ = v.make_regions(data, detections, policy, [])
        self.assertFalse(v.audit_source_frames(data, scan, policy, [], raw))

    def test_false_positive_exclusions_are_local_and_do_not_disable_real_hits(self):
        hit = {
            "type": "email",
            "box": [20, 40, 200, 65],
            "reason": "weak-known-email-ocr-variant",
        }
        policy = copy.deepcopy(v.DEFAULT_POLICY)
        policy["review_exclusions"] = [
            {
                "start": 1,
                "end": 1.04,
                "type": "email",
                "box": [10, 30, 210, 75],
                "reason": "Confirmed ordinary text in synthetic fixture",
            }
        ]
        self.assertTrue(v.review_excluded(hit, policy, 1))
        self.assertFalse(v.review_excluded(hit, policy, 1.04))
        self.assertFalse(
            v.review_excluded(dict(hit, box=[20, 140, 200, 165]), policy, 1)
        )
        self.assertFalse(
            v.review_excluded(dict(hit, reason="email-pattern"), policy, 1)
        )

    def test_render_blocks_unresolved_source_candidates_before_encoding(self):
        with tempfile.TemporaryDirectory(prefix="privacy-render-guard-") as folder:
            root = Path(folder)
            revision = {
                "revision": "r0001",
                "policy": v.DEFAULT_POLICY,
                "rules": [],
                "required_regions": [[]],
            }
            data = {"pts": [0]}
            finding = {"frame": 0, "time": 0, "type": "email", "box": [0, 0, 10, 10]}
            args = SimpleNamespace(
                project=folder, revision="r0001", output=str(root / "out.mp4")
            )
            with patch.object(v, "project", return_value=(root, data)), patch.object(
                v, "load_revision", return_value=revision
            ), patch.object(v, "records", return_value=iter([])), patch.object(
                v, "audit_source_frames", return_value=[finding]
            ), patch.object(
                v, "run"
            ) as run:
                with self.assertRaisesRegex(
                    RuntimeError, "Uncovered source candidates"
                ):
                    v.cmd_render(args)
                run.assert_not_called()
            self.assertEqual(
                v.read(root / "reports/r0001-planning.json")["source_findings"],
                [finding],
            )

    def test_union_coverage_does_not_double_count_masks(self):
        self.assertAlmostEqual(
            v.coverage([0, 0, 100, 20], [[0, 0, 60, 20], [40, 0, 100, 20]]), 1
        )
        self.assertAlmostEqual(
            v.coverage([0, 0, 100, 20], [[0, 0, 60, 20], [0, 0, 60, 20]]), 0.6
        )

    def test_decoder_edit_list_alignment(self):
        data = {"pts": [0.023, 0.064, 0.106]}
        rows = [{"pts": t, "frame": i} for i, t in enumerate([0, 0.023, 0.064, 0.106])]
        aligned = v.align_scan(data, rows, 0)
        self.assertEqual([r["frame"] for r in aligned], [0, 1, 2])
        with self.assertRaises(RuntimeError):
            v.align_scan(data, rows[:-1], 0)

    def test_unused_region_does_not_erase_top_left_pixel(self):
        with tempfile.TemporaryDirectory(prefix="privacy-corner-") as folder:
            root = Path(folder)
            plan = {
                "regions": [[[0, 0, 20, 20], [40, 40, 60, 60]], [[0, 0, 20, 20]]],
                "policy": {},
            }
            graph = v.graph(
                {"pts": [0, 0.1]}, plan, root / "commands.txt", mask_only=True
            )
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=black:s=80x80:r=10:d=0.2",
                    "-filter_complex",
                    graph,
                    "-map",
                    "[m]",
                    "-pix_fmt",
                    "gray",
                    "-f",
                    "rawvideo",
                    "pipe:1",
                ],
                cwd=root,
                capture_output=True,
                check=True,
            )
            self.assertEqual(len(result.stdout), 2 * 80 * 80)
            self.assertEqual(result.stdout[80 * 80], 255)

    def test_source_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="privacy-source-") as folder:
            root = Path(folder)
            source = root / "source.bin"
            source.write_bytes(b"original")
            v.write(
                root / "project.json",
                {
                    "source": str(source),
                    "source_size": 8,
                    "source_sha256": v.digest(source),
                },
            )
            v.project(root)
            source.write_bytes(b"changed!")
            with self.assertRaises(RuntimeError):
                v.project(root)


class EnvironmentTests(unittest.TestCase):
    """Run the bootstrap against fake tools; never install system packages in tests."""

    DRIVER = r"""#!/bin/bash
name="${0##*/}"
printf '%s|%s\n' "$name" "$*" >> "$VP_TEST_ROOT/calls"
case "$name" in
    uname)
        if [ "$1" = -s ]; then printf '%s\n' "${VP_TEST_OS:-Darwin}"; else printf 'arm64\n'; fi ;;
    xcode-select)
        if [ "$1" = --install ]; then exit 0; fi
        [ "${VP_TEST_NO_CLT:-0}" = 0 ] || exit 1
        printf '/fake/developer\n' ;;
    xcrun) printf '/fake/swiftc\n' ;;
    swiftc) printf 'Swift test\n' ;;
    brew)
        if [ "$1" = --prefix ]; then printf '%s/prefix\n' "$VP_TEST_ROOT"; exit 0; fi
        [ "$1" = install ] || exit 90
        [ "${VP_TEST_FAIL_INSTALL:-0}" = 0 ] || exit 1
        shift
        for package in "$@"; do printf 'installed' > "$VP_TEST_ROOT/$package"; done ;;
    python3)
        if [ "$1" = -c ]; then
            if [ "${VP_TEST_NO_PYTHON:-0}" = 1 ] && [ ! -f "$VP_TEST_ROOT/python" ]; then exit 1; fi
        else
            [ "$2" = doctor ] && [ "$3" = --full ] || exit 91
            [ "${VP_TEST_FAIL_DOCTOR:-0}" = 0 ] || exit 2
            printf '{"ready":true,"full_check":true}\n'
        fi ;;
    ffmpeg|ffprobe)
        if [ "${VP_TEST_NO_MEDIA:-0}" = 1 ] && [ ! -f "$VP_TEST_ROOT/ffmpeg" ]; then exit 1; fi
        if [ "$name" = ffprobe ] && [ "${VP_TEST_NO_PROBE:-0}" = 1 ] && [ ! -f "$VP_TEST_ROOT/ffmpeg" ]; then exit 1; fi ;;
    curl) printf 'Unexpected network call in fixture\n' >&2; exit 92 ;;
    *) exit 93 ;;
esac
"""

    def fixture(self, folder):
        root = Path(folder)
        tools = root / "tools"
        tools.mkdir()
        (root / "prefix" / "bin").mkdir(parents=True)
        (root / "prefix" / "sbin").mkdir()
        for name in (
            "uname",
            "xcode-select",
            "xcrun",
            "swiftc",
            "brew",
            "python3",
            "ffmpeg",
            "ffprobe",
            "curl",
        ):
            path = tools / name
            path.write_text(self.DRIVER)
            path.chmod(0o755)
        return dict(os.environ, PATH=f"{tools}:/usr/bin:/bin", VP_TEST_ROOT=str(root))

    def bootstrap(self, environment, mode="--check"):
        result = subprocess.run(
            ["/bin/bash", str(HERE / "setup.sh"), mode],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        calls = (Path(environment["VP_TEST_ROOT"]) / "calls").read_text().splitlines()
        return result, calls

    def test_non_macos_stops_before_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.fixture(folder)
            env["VP_TEST_OS"] = "Linux"
            result, calls = self.bootstrap(env, "--install")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(calls, ["uname|-s"])

    def test_check_missing_clt_never_launches_installation_or_python_stub(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.fixture(folder)
            env["VP_TEST_NO_CLT"] = "1"
            result, calls = self.bootstrap(env)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("xcode-select|--install", calls)
            self.assertFalse(
                any(
                    c.startswith("python3|") or c.startswith("brew|install")
                    for c in calls
                )
            )

    def test_clt_request_is_pending_not_success(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.fixture(folder)
            env["VP_TEST_NO_CLT"] = "1"
            result, calls = self.bootstrap(env, "--install")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(calls.count("xcode-select|--install"), 1)
            self.assertNotIn("Environment verified", result.stderr)

    def test_check_missing_python_does_not_install(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.fixture(folder)
            env["VP_TEST_NO_PYTHON"] = "1"
            result, calls = self.bootstrap(env)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(any(c.startswith("brew|install") for c in calls))

    def test_install_only_missing_python_then_verify_and_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.fixture(folder)
            env["VP_TEST_NO_PYTHON"] = "1"
            result, calls = self.bootstrap(env, "--install")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("brew|install python", calls)
            self.assertFalse(
                any(c.startswith("brew|install") and "ffmpeg" in c for c in calls)
            )
            self.assertTrue(calls[-1].endswith("doctor --full"))
            before = len(calls)
            result, calls = self.bootstrap(env, "--install")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(any(c.startswith("brew|install") for c in calls[before:]))

    def test_missing_ffprobe_installs_ffmpeg_package(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.fixture(folder)
            env["VP_TEST_NO_PROBE"] = "1"
            result, calls = self.bootstrap(env, "--install")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("brew|install ffmpeg", calls)
            self.assertNotIn("brew|install python", calls)

    def test_install_failure_does_not_continue_to_doctor(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.fixture(folder)
            env.update(VP_TEST_NO_MEDIA="1", VP_TEST_FAIL_INSTALL="1")
            result, calls = self.bootstrap(env, "--install")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any(c.endswith("doctor --full") for c in calls))
            self.assertNotIn("Environment verified", result.stderr)

    def test_failed_full_verification_does_not_claim_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.fixture(folder)
            env["VP_TEST_FAIL_DOCTOR"] = "1"
            result, calls = self.bootstrap(env, "--install")
            self.assertEqual(result.returncode, 2)
            self.assertTrue(calls[-1].endswith("doctor --full"))
            self.assertNotIn("Environment verified", result.stderr)


def execute(*args, expect=0):
    r = subprocess.run([str(x) for x in args])
    if r.returncode != expect:
        raise RuntimeError(
            f"Command failed: {Path(str(args[0])).name}, exit {r.returncode}"
        )


def cli(*args, expect=0):
    execute("python3", HERE / "video_privacy.py", *args, expect=expect)


def e2e(root):
    root.mkdir(parents=True, exist_ok=True)
    executable = root / "make-images"
    execute("swiftc", "-O", HERE / "make_test_images.swift", "-o", executable)
    execute(executable, root / "images")
    clips = []
    for i in range(3):
        clip = root / f"phase{i}.mp4"
        clips.append(clip)
        execute(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-loop",
            "1",
            "-framerate",
            "24",
            "-i",
            root / "images" / f"phase{i}.png",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "2",
            "-vf",
            "crop=480:360:0:100+30*sin(t*2)",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            clip,
        )
    listing = root / "clips.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in clips))
    source = root / "synthetic.mp4"
    execute(
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        listing,
        "-c",
        "copy",
        source,
    )
    project = root / "project"
    cli("init", "--input", source, "--project", project)
    policy = v.read(project / "policy.json")
    policy["known_names"] = ["Sample User"]
    # Ground-truth tiny field intentionally moves through the lower viewport edge.
    policy["manual_regions"] = [
        {
            "start": 4,
            "end": v.read(project / "project.json")["duration"],
            "type": "email",
            "box": [232, 318, 365, 360],
        }
    ]
    v.write(project / "policy.json", policy)
    cli("scan", "--project", project, "--chunk-seconds", "1.5")
    # Simulate an interrupted scan with valid checkpoints and an unfinished last block.
    chunks = sorted((project / "cache").glob("scan-chunks-*/*.jsonl"))
    before = {part: part.stat().st_mtime_ns for part in chunks[:-1]}
    expected = list(v.records(project / "cache/scan.jsonl"))
    chunks[-1].unlink()
    chunks[-1].with_suffix(".meta.json").unlink()
    (project / "cache/scan.jsonl").unlink()
    (project / "cache/scan-meta.json").unlink()
    cli("scan", "--project", project, "--chunk-seconds", "1.5")
    assert all(
        part.stat().st_mtime_ns == stamp for part, stamp in before.items()
    ), "Resume must reuse verified checkpoints"
    assert (
        list(v.records(project / "cache/scan.jsonl")) == expected
    ), "Resumed scan must preserve every original frame and PTS"
    cli("plan", "--project", project)
    scan = list(v.records(project / "cache" / "scan.jsonl"))
    assert any(
        v.EMAIL.search(t["text"]) for f in scan for t in f["texts"]
    ), "Fixture email was not recognized"
    revision = v.read(project / "revisions" / "r0001.json")
    assert any(
        revision["regions"][i]
        for i, t in enumerate(v.read(project / "project.json")["pts"])
        if t < 1.5
    ), "Partial prefix not protected"
    cli("render", "--project", project)
    cli("verify", "--project", project)
    report = v.read(project / "reports" / "r0001.json")
    assert report["ocr_complete"] and report["ocr_frames_checked"] == len(
        scan
    ), "Default verification must inspect every output frame"
    # An actual one-frame privacy leak must fail full verification even when the
    # planned mask is intact. This frame falls between half-second sample times.
    clean = v.read(project / "renders" / "r0001.json")
    leaky = root / "single-frame-leak.mp4"
    execute(
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-copyts",
        "-start_at_zero",
        "-i",
        clean["output"],
        "-i",
        source,
        "-filter_complex",
        r"[0:v][1:v]blend=all_expr=if(eq(N\,38)\,B\,A)[v]",
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-crf",
        "19",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "passthrough",
        "-enc_time_base",
        "1/1000000",
        "-c:a",
        "copy",
        leaky,
    )
    leak_revision = copy.deepcopy(revision)
    leak_revision["revision"] = "r0000"
    v.write(project / "revisions" / "r0000.json", leak_revision)
    v.write(
        project / "renders" / "r0000.json",
        dict(clean, revision="r0000", output=str(leaky), output_sha256=v.digest(leaky)),
    )
    cli("verify", "--project", project, "--revision", "r0000", expect=2)
    leak_report = v.read(project / "reports" / "r0000.json")
    assert (
        leak_report["ocr_complete"]
        and len({f["frame"] for f in leak_report["ocr_findings"]}) == 1
    ), "Full OCR must catch the single-frame leak"
    cli("verify", "--project", project, "--revision", "r0000", "--sample-seconds", ".5")
    partial = v.read(project / "reports" / "r0000.json")
    assert (
        not partial["ocr_complete"] and not partial["ocr_findings"]
    ), "Quick sampling must be explicitly reported as incomplete"
    # Keep the final fixture report as the complete, failed verification.
    v.write(project / "reports" / "r0000.json", leak_report)
    # One-frame OCR corruption at one of two same-type fields must not remove its mask.
    data = v.read(project / "project.json")
    hints = v.hints_for(scan, policy)
    index = next(
        i
        for i, t in enumerate(data["pts"])
        if 2.5 < t < 3
        and len(
            [h for h in v.detect_frame(scan[i], policy, hints) if h["type"] == "email"]
        )
        >= 2
    )
    altered = copy.deepcopy(scan)
    target = next(
        row for row in altered[index]["texts"] if row["text"].startswith("demo.")
    )
    target["text"] = "dem0.uslrCexamp1e.com"
    detections = [v.detect_frame(frame, policy, hints) for frame in altered]
    required, _ = v.make_regions(data, detections, policy, [])
    assert (
        v.coverage(target["box"], required[index]) >= 0.98
    ), "The second email must remain protected during a one-frame OCR miss"
    assert not v.audit_source_frames(
        data, altered, policy, [], required
    ), "Covered OCR corruption must pass the source-instance audit"
    cli(
        "revise",
        "--project",
        project,
        "--base",
        "r0001",
        "--start",
        "00:02",
        "--end",
        "end",
        "--keep",
        "email",
    )
    cli(
        "revise",
        "--project",
        project,
        "--base",
        "r0002",
        "--start",
        "00:00",
        "--end",
        "00:01",
        "--clear",
    )
    cli("render", "--project", project, "--revision", "r0003")
    cli("verify", "--project", project, "--revision", "r0003")
    # The parent rules and render stay intact after a branch edit.
    assert v.read(project / "revisions" / "r0001.json") == revision
    vfr = root / "vfr.mp4"
    execute(
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        source,
        "-vf",
        r"select=not(mod(n\,3))+between(n\,48\,95)",
        "-an",
        "-fps_mode",
        "vfr",
        "-c:v",
        "libx264",
        vfr,
    )
    vp = root / "vfr-project"
    cli("init", "--input", vfr, "--project", vp)
    vfr_policy = copy.deepcopy(policy)
    vfr_policy["manual_regions"][0]["end"] = v.read(vp / "project.json")["duration"]
    v.write(vp / "policy.json", vfr_policy)
    cli("scan", "--project", vp)
    cli("plan", "--project", vp)
    cli("render", "--project", vp)
    cli("verify", "--project", vp)
    rotated = root / "rotated.mp4"
    execute(
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-display_rotation",
        "90",
        "-i",
        source,
        "-c",
        "copy",
        rotated,
    )
    rp = root / "rotation-project"
    cli("init", "--input", rotated, "--project", rp)
    assert (
        v.read(rp / "project.json")["width"],
        v.read(rp / "project.json")["height"],
    ) == (360, 480)
    rotation_policy = copy.deepcopy(policy)
    rotation_policy["manual_regions"][0]["end"] = v.read(rp / "project.json")[
        "duration"
    ]
    rotation_policy["manual_regions"][0]["box"] = [318, 115, 360, 248]
    v.write(rp / "policy.json", rotation_policy)
    cli("scan", "--project", rp)
    cli("plan", "--project", rp)
    cli("render", "--project", rp)
    cli("verify", "--project", rp)
    # Real Vision Code128 detection, Traditional Chinese, and mixed solid/blur output.
    for name in ("payment", "product"):
        clip = root / f"{name}.mp4"
        execute(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-loop",
            "1",
            "-framerate",
            "12",
            "-i",
            root / "images" / f"{name}.png",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            clip,
        )
        feature = root / f"{name}-project"
        cli("init", "--input", clip, "--project", feature)
        fp = v.read(feature / "policy.json")
        fp["types"] = ["payment_code", "name"]
        fp["type_modes"] = {"payment_code": "solid"}
        v.write(feature / "policy.json", fp)
        cli("scan", "--project", feature)
        cli("plan", "--project", feature)
        fs = list(v.records(feature / "cache/scan.jsonl"))
        assert all(
            "zh-Hant" in frame["languages"] for frame in fs
        ), "Traditional Chinese must be enabled"
        assert any(
            "code128" in code["symbology"].lower()
            for frame in fs
            for code in frame["codes"]
        ), "Code128 must be detected"
        assert all(
            set(code) <= {"box", "confidence", "symbology"}
            for frame in fs
            for code in frame["codes"]
        ), "Barcode payload must not enter caches"
        fr = v.read(feature / "revisions/r0001.json")
        if name == "payment":
            assert any(fr["layers"]["solid"]) and any(
                fr["layers"]["gaussian"]
            ), "Payment and name need separate rendering modes"
            cli("render", "--project", feature)
            cli("verify", "--project", feature)
        else:
            assert fr["review_findings"] and not any(
                fr["regions"]
            ), "Ordinary barcodes need review, not automatic payment masks"
            cli("render", "--project", feature, expect=1)
    print("End-to-end fixtures passed:", root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--e2e", action="store_true")
    parser.add_argument("--workdir")
    args = parser.parse_args()
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromTestCase(case)
        for case in (BehaviorTests, EnvironmentTests)
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    if args.e2e:
        directory = (
            Path(args.workdir).resolve()
            if args.workdir
            else Path(tempfile.mkdtemp(prefix="video-privacy-test-"))
        )
        e2e(directory)
