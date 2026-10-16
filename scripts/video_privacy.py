#!/usr/bin/env python3
"""Local, timestamp-driven video privacy editing; Python standard library only."""

import argparse
import bisect
import copy
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import statistics

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
TYPES = {
    "email",
    "phone",
    "name",
    "id",
    "card",
    "order",
    "verification",
    "payment_code",
    "custom",
}
PLANNER_VERSION = 3
DEFAULT_POLICY = {
    "types": sorted(TYPES),
    "known_names": [],
    "known_values": {},
    "manual_regions": [],
    "review_exclusions": [],
    "sigma": 20,
    "smooth": True,
    "padding": 8,
    "feather": 3,
    "mode": "gaussian",
    "type_modes": {},
    "jitter_pixels": 4,
    "ocr_languages": ["zh-Hans", "zh-Hant", "en-US"],
}


def fail(message):
    raise RuntimeError(message)


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)
    path.chmod(0o600)


def run(args, capture=False, cwd=None):
    result = subprocess.run(
        [str(a) for a in args], capture_output=capture, text=capture, cwd=cwd
    )
    if result.returncode:
        fail(
            f"{Path(str(args[0])).name} failed (exit {result.returncode})"
            + (": " + result.stderr[-1600:] if capture else "")
        )
    return result.stdout if capture else None


def binary(name):
    path = shutil.which(name)
    if not path:
        fail(
            f"Missing dependency: {name}. Install the missing tool using the current environment permissions, then retry."
        )
    return path


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def timestamp(value, duration=None):
    text = re.sub(r"[\s`]+", "", str(value)).replace("：", ":")
    if text.lower() in {"end", "结束", "最后"}:
        if duration is None:
            fail("end needs a known video duration")
        return duration
    pieces = text.split(":")
    if not 1 <= len(pieces) <= 3:
        fail("Use seconds, MM:SS, HH:MM:SS, or end")
    try:
        parts = [float(x) for x in pieces]
    except ValueError:
        fail("Invalid time value")
    if any(x < 0 or not math.isfinite(x) for x in parts) or any(
        x >= 60 for x in parts[1:]
    ):
        fail("Invalid time component")
    return sum(x * 60**i for i, x in enumerate(reversed(parts)))


def interval(start, end, duration):
    a, b = timestamp(start, duration), timestamp(end, duration)
    if not 0 <= a < b <= duration + 0.002:
        fail("Time interval must satisfy 0 <= start < end <= video duration")
    return a, min(b, duration)


def selected(value):
    values = set(value.split(",")) if value else set()
    if values - TYPES:
        fail("Unknown privacy type: " + ",".join(sorted(values - TYPES)))
    return values


def probe(path):
    return json.loads(
        run(
            [
                binary("ffprobe"),
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                path,
            ],
            True,
        )
    )


def timeline(path, start):
    info = json.loads(
        run(
            [
                binary("ffprobe"),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp_time,pkt_duration_time,duration_time",
                "-of",
                "json",
                path,
            ],
            True,
        )
    )
    frames = info.get("frames", [])
    if not frames:
        fail("No decoded video frames")
    pts = [float(f["best_effort_timestamp_time"]) - start for f in frames]
    if pts[0] < -0.002 or any(b <= a for a, b in zip(pts, pts[1:])):
        fail("Unsupported nonmonotonic or negative video timestamps")
    return [max(0, t) for t in pts]


def project(folder):
    root = Path(folder).expanduser().resolve()
    data = read(root / "project.json")
    source = Path(data["source"])
    if (
        not source.is_file()
        or source.stat().st_size != data["source_size"]
        or digest(source) != data["source_sha256"]
    ):
        fail(
            "Source is missing or changed; use the original source or initialize a new project"
        )
    return root, data


def paths(folder):
    return Path(folder).expanduser().resolve()


def helper(root):
    if sys.platform != "darwin":
        fail("Apple Vision OCR requires macOS; no cloud fallback is used")
    source = HERE / "vision_ocr.swift"
    key = digest(source)[:12]
    target = root / "cache" / ("vision-" + key)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        run([binary("swiftc"), "-O", source, "-o", target])
    return target


def align_scan(data, rows, start, sampled=False):
    target = [t + start for t in data["pts"]]
    aligned = {}
    for row in rows:
        pos = bisect.bisect_left(target, row["pts"])
        choices = [k for k in (pos - 1, pos) if 0 <= k < len(target)]
        i = min(choices, key=lambda k: abs(target[k] - row["pts"]))
        if abs(target[i] - row["pts"]) > 0.002:
            # AVFoundation may synthesize an edit-list blank before actual video.
            if row["pts"] < target[0] or row["pts"] > target[-1]:
                continue
            fail("OCR decoder presentation time does not match source video")
        if i in aligned:
            fail("OCR decoder produced duplicate presentation timestamps")
        row["frame"] = i
        aligned[i] = row
    if not sampled and len(aligned) != len(target):
        fail("OCR is missing source presentation timestamps")
    return [aligned[i] for i in sorted(aligned)]


def scan_video(
    root,
    source,
    destination,
    sample=0,
    data=None,
    start=None,
    languages=None,
    chunk_seconds=30,
    force=False,
):
    languages = languages or DEFAULT_POLICY["ocr_languages"]
    start = (
        data.get("source_start_time", 0)
        if data is not None and start is None
        else start
    )
    if data is not None and sample == 0:
        return resumable_scan(
            root, source, destination, data, start, languages, chunk_seconds, force
        )
    partial = destination.with_name(destination.name + ".partial")
    run([helper(root), source, partial, str(sample), "0", "inf", ",".join(languages)])
    if data is not None:
        rows = align_scan(
            data,
            list(records(partial)),
            data["source_start_time"] if start is None else start,
            sample > 0,
        )
        with partial.open("w") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    partial.replace(destination)


def write_records(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def valid_chunk(part, meta, identity, data, start, first, last):
    try:
        stamp = read(meta)
        if stamp.get("identity") != identity or stamp.get("sha256") != digest(part):
            return False
        rows = align_scan(data, list(records(part)), start, True)
        return [r["frame"] for r in rows] == list(range(first, last)) and all(
            (r["width"], r["height"]) == (data["width"], data["height"]) for r in rows
        )
    except (OSError, ValueError, KeyError, RuntimeError):
        return False


def resumable_scan(
    root, source, destination, data, start, languages, chunk_seconds=30, force=False
):
    if not math.isfinite(chunk_seconds) or chunk_seconds <= 0:
        fail("Chunk seconds must be finite and positive")
    identity = {
        "source_sha256": digest(source),
        "helper_sha256": digest(HERE / "vision_ocr.swift"),
        "languages": languages,
        "start": start,
        "chunk_seconds": chunk_seconds,
        "pts_sha256": hashlib.sha256(json.dumps(data["pts"]).encode()).hexdigest(),
    }
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    folder = root / "cache" / f"{destination.stem}-chunks-{key}"
    folder.mkdir(parents=True, exist_ok=True)
    pts = data["pts"]
    parts = []
    first = 0
    reused = 0
    while first < len(pts):
        last = max(first + 1, bisect.bisect_left(pts, pts[first] + chunk_seconds))
        part = folder / f"{first:08d}-{last:08d}.jsonl"
        meta = part.with_suffix(".meta.json")
        valid = False
        if part.exists() and meta.exists() and not force:
            valid = valid_chunk(part, meta, identity, data, start, first, last)
        if valid:
            reused += 1
        else:
            partial = part.with_name(part.name + ".partial")
            begin = max(0, start + pts[first] - 0.00001)
            end = (
                start + pts[last] + 0.002
                if last < len(pts)
                else start + data["duration"] + 0.01
            )
            run(
                [
                    helper(root),
                    source,
                    partial,
                    "0",
                    str(begin),
                    str(end),
                    ",".join(languages),
                    str(start + pts[first]),
                ]
            )
            rows = align_scan(data, list(records(partial)), start, True)
            rows = [row for row in rows if first <= row["frame"] < last]
            if [r["frame"] for r in rows] != list(range(first, last)):
                fail("OCR chunk has missing or unexpected presentation timestamps")
            if any(
                (r["width"], r["height"]) != (data["width"], data["height"])
                for r in rows
            ):
                fail("OCR chunk display dimensions differ")
            write_records(part, rows)
            partial.unlink(missing_ok=True)
            write(
                meta,
                {
                    "identity": identity,
                    "sha256": digest(part),
                    "first": first,
                    "last": last,
                },
            )
        parts.append(part)
        first = last
        print(
            f"OCR checkpoint: {last}/{len(pts)} frames; reused chunks: {reused}",
            flush=True,
        )
    write_records(destination, (row for part in parts for row in records(part)))
    check_scan(data, list(records(destination)))
    write(
        destination.with_suffix(".checkpoint.json"),
        {
            "identity": identity,
            "chunks": len(parts),
            "reused_chunks": reused,
            "frames": len(pts),
        },
    )


def records(path):
    with Path(path).open() as stream:
        for line in stream:
            yield json.loads(line)


def check_scan(data, scan):
    if len(scan) != len(data["pts"]):
        fail("OCR frame count differs from the source timeline")
    if any(r["frame"] != i for i, r in enumerate(scan)):
        fail("OCR frame order is incomplete")
    dimensions = {(r["width"], r["height"]) for r in scan}
    if dimensions != {(data["width"], data["height"])}:
        fail("OCR orientation/dimensions differ from displayed video")


EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+\s*@\s*[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE = re.compile(
    r"(?<!\d)(?:\+?86[ -]?)?1[3-9](?:[ -]?\d){9}(?!\d)|(?<!\d)0\d{2,3}[ -]\d{7,8}(?!\d)|\+\d[\d -]{8,18}\d"
)
NUMBER = re.compile(r"(?<!\d)\d(?:[ -]?\d){3,}(?!\d)")
NAME = re.compile(
    r"(?:姓名|投保人|被保险人|被保險人|被保人|联系人|聯絡人|聯繫人|收件人|持卡人)[：: ]+([\u3400-\u9fff]{2,4})"
)
VERIFY = re.compile(r"验证码|校验码|动态码|verification|one.time|\botp\b", re.I)
ORDER = re.compile(r"订单|保单|通知单|订单号|order|policy\s*(?:no|number)", re.I)
CARD = re.compile(r"银行卡|卡号|尾号|借记卡|信用卡|card|ending", re.I)
PAYMENT = re.compile(
    r"付款[码碼]|支付[码碼]|向商家付|付款條[码碼]|payment\s*code|pay\s*(?:a\s*)?merchant",
    re.I,
)


def utf16(text, index):
    return len(text[:index].encode("utf-16-le")) // 2


def range_box(observation, start, end):
    a, b = utf16(observation["text"], start), utf16(observation["text"], end)
    exact = [
        t["box"]
        for t in observation.get("tokens", [])
        if t["start"] == a and t["end"] == b
    ]
    if exact:
        return exact[0]
    tokens = [
        t["box"]
        for t in observation.get("tokens", [])
        if t["start"] < b and t["end"] > a
    ]
    if not tokens:
        return observation["box"]
    return [
        min(t[0] for t in tokens),
        min(t[1] for t in tokens),
        max(t[2] for t in tokens),
        max(t[3] for t in tokens),
    ]


def hints_for(scan, policy):
    hints = {t: set(values) for t, values in policy.get("known_values", {}).items()}
    hints.setdefault("name", set()).update(policy.get("known_names", []))
    for frame in scan:
        for row in frame["texts"]:
            for match in EMAIL.finditer(row["text"]):
                hints.setdefault("email", set()).add(re.sub(r"\s+", "", match.group()))
            for match in PHONE.finditer(row["text"]):
                hints.setdefault("phone", set()).add(re.sub(r"\D", "", match.group()))
            for match in NAME.finditer(row["text"]):
                hints.setdefault("name", set()).add(match.group(1))
    return hints


def detect_frame(frame, policy, hints):
    found = []
    seen = set()
    context = " ".join(row["text"] for row in frame["texts"])

    def add(row, start, end, kind, reason, identity=None):
        if kind not in TYPES:
            fail("Unknown hint privacy type")
        box = range_box(row, start, end)
        key = (kind, tuple(round(v, 1) for v in box))
        if key not in seen:
            value = identity or row["text"][start:end]
            value = (
                re.sub(r"\D", "", value)
                if kind == "phone"
                else re.sub(r"\s+", "", value).lower()
            )
            identity = hashlib.sha256((kind + ":" + value).encode()).hexdigest()[:20]
            seen.add(key)
            found.append(
                {
                    "type": kind,
                    "box": box,
                    "confidence": row.get("confidence", 0),
                    "reason": reason,
                    "identity": identity,
                }
            )

    for row in frame["texts"]:
        text = row["text"]
        for match in EMAIL.finditer(text):
            add(row, *match.span(), "email", "email-pattern")
        for match in PHONE.finditer(text):
            add(row, *match.span(), "phone", "phone-pattern")
        for match in NAME.finditer(text):
            add(row, *match.span(1), "name", "name-label")
        for match in NUMBER.finditer(text):
            value = re.sub(r"\D", "", match.group())
            near = text[max(0, match.start() - 24) : match.end() + 24]
            if 16 <= len(value) <= 24 and PAYMENT.search(context):
                kind = "payment_code"
            elif len(value) == 11 and value.startswith("1"):
                kind = "phone"
            elif len(value) == 18 and re.match(r"^[1-9]\d{5}(?:19|20)\d{2}", value):
                kind = "id"
            elif len(value) >= 12:
                kind = (
                    "order"
                    if ORDER.search(near) or ORDER.search(context)
                    else "card" if CARD.search(near) else "order"
                )
            elif len(value) == 4 and CARD.search(near):
                kind = "card"
            elif 4 <= len(value) <= 8 and (
                VERIFY.search(context)
                or len(value) == 6
                and text.strip() == match.group().strip()
            ):
                kind = "verification"
            else:
                continue
            add(row, *match.span(), kind, "numeric-context")
        # Standalone identity number with X checksum.
        for match in re.finditer(r"(?<!\d)[1-9]\d{16}[0-9Xx](?!\d)", text):
            add(row, *match.span(), "id", "identity-number")
        for kind, values in hints.items():
            for value in sorted(values):
                if not value:
                    continue
                for match in re.finditer(re.escape(value), text):
                    add(row, *match.span(), kind, "known-value")
                # A later full value can identify its earlier partially typed prefix.
                minimum = (
                    3
                    if kind in {"email", "phone"}
                    else 1 if kind == "name" else len(value)
                )
                stripped = text.strip()
                if minimum <= len(stripped) < len(value) and value.lower().startswith(
                    stripped.lower()
                ):
                    pos = text.index(stripped)
                    add(row, pos, pos + len(stripped), kind, "partial-input", value)
                if kind == "email":
                    for match in re.finditer(
                        r"[A-Za-z0-9._%+\-]{3,}(?:@[A-Za-z0-9.\-]*)?", text
                    ):
                        if len(match.group()) >= 3 and value.lower().startswith(
                            match.group().lower()
                        ):
                            add(row, *match.span(), kind, "email-prefix", value)
                    for match in re.finditer(r"[A-Za-z0-9._%+@\-]{8,}", text):
                        candidate = match.group().lower().rstrip(".")
                        if (
                            0.75 < len(candidate) / len(value) < 1.3
                            and difflib.SequenceMatcher(
                                None, candidate, value.lower()
                            ).ratio()
                            >= 0.88
                        ):
                            add(
                                row,
                                *match.span(),
                                kind,
                                "known-email-ocr-variant",
                                value,
                            )
                if kind == "phone":
                    for match in re.finditer(r"\d{3,}", text):
                        if value.startswith(match.group()):
                            add(row, *match.span(), kind, "phone-prefix", value)
    for code in frame.get("codes", []):
        if PAYMENT.search(context):
            found.append(
                {
                    "type": "payment_code",
                    "box": code["box"],
                    "confidence": code.get("confidence", 0),
                    "reason": "payment-context-barcode",
                }
            )
    return found


def contains(a, b):
    return a[0] <= b[0] and a[1] <= b[1] and a[2] >= b[2] and a[3] >= b[3]


def clamp(box, width, height, padding=0):
    a, b, c, d = box
    return [
        max(0, math.floor(a) - padding),
        max(0, math.floor(b) - padding),
        min(width, math.ceil(c) + padding),
        min(height, math.ceil(d) + padding),
    ]


def effective(policy, rules, t):
    active = set(policy["types"])
    for rule in rules:
        if rule["start"] <= t < rule["end"]:
            if rule["action"] == "keep":
                active = set(rule["types"])
            elif rule["action"] == "remove":
                active -= set(rule["types"])
            elif rule["action"] == "set":
                active = set(rule["types"])
    return active


def coverage(box, regions):
    """Union coverage, so adjacent masks cannot double-count their overlap."""
    x0, y0, x1, y1 = box
    area = (x1 - x0) * (y1 - y0)
    if area <= 0:
        return 0
    pieces = []
    for a, b, c, d in regions:
        a, b, c, d = max(a, x0), max(b, y0), min(c, x1), min(d, y1)
        if a < c and b < d:
            pieces.append((a, b, c, d))
    xs = sorted({x for p in pieces for x in (p[0], p[2])})
    covered = 0
    for left, right in zip(xs, xs[1:]):
        spans = sorted((b, d) for a, b, c, d in pieces if a <= left and c >= right)
        end = -math.inf
        height = 0
        for b, d in spans:
            height += max(0, d - max(b, end))
            end = max(end, d)
        covered += (right - left) * height
    return min(1, covered / area)


def weak_email_candidates(frame, hints, strong):
    """Review only: tolerate OCR corruption without broadly auto-blurring text."""
    candidates = []
    seen = set()
    for row in frame["texts"]:
        for match in re.finditer(r"[A-Za-z0-9._%+@\-]{8,}", row["text"]):
            text = match.group().lower().rstrip(".")
            similar = any(
                0.55 < len(text) / len(value) < 1.35
                and difflib.SequenceMatcher(None, text, value.lower()).ratio() >= 0.72
                for value in hints.get("email", [])
                if value
            )
            if not similar:
                continue
            box = range_box(row, *match.span())
            key = tuple(round(x, 1) for x in box)
            if (
                key in seen
                or coverage(box, [h["box"] for h in strong if h["type"] == "email"])
                >= 0.98
            ):
                continue
            seen.add(key)
            candidates.append(
                {"type": "email", "box": box, "reason": "weak-known-email-ocr-variant"}
            )
    return candidates


def review_candidates(frame, hints, strong):
    candidates = weak_email_candidates(frame, hints, strong)
    if not PAYMENT.search(" ".join(row["text"] for row in frame["texts"])):
        for code in frame.get("codes", []):
            candidates.append(
                {
                    "type": "payment_code",
                    "box": code["box"],
                    "reason": "weak-barcode-purpose",
                }
            )
        if frame.get("codes"):
            for row in frame["texts"]:
                for match in NUMBER.finditer(row["text"]):
                    if 16 <= len(re.sub(r"\D", "", match.group())) <= 24:
                        candidates.append(
                            {
                                "type": "payment_code",
                                "box": range_box(row, *match.span()),
                                "reason": "weak-payment-number",
                            }
                        )
    return candidates


def review_excluded(hit, policy, t):
    return hit["reason"].startswith("weak-") and any(
        e["start"] <= t < e["end"]
        and e["type"] == hit["type"]
        and contains(e["box"], hit["box"])
        for e in policy.get("review_exclusions", [])
    )


def audit_source_frames(data, scan, policy, rules, regions):
    check_scan(data, scan)
    hints = hints_for(scan, policy)
    findings = []
    for i, frame in enumerate(scan):
        t = data["pts"][i]
        active = effective(policy, rules, t)
        strong = detect_frame(frame, policy, hints)
        for hit in strong + review_candidates(frame, hints, strong):
            if hit["type"] not in active or coverage(hit["box"], regions[i]) >= 0.98:
                continue
            # Explicit, localized false-positive decisions apply only to weak candidates.
            if not review_excluded(hit, policy, t):
                findings.append(
                    {
                        "frame": i,
                        "time": t,
                        "type": hit["type"],
                        "box": hit["box"],
                        "reason": hit["reason"],
                    }
                )
    return findings


def field_distance(a, b, data):
    if a["type"] != b["type"]:
        return None
    if a.get("identity") and b.get("identity") and a["identity"] != b["identity"]:
        return None
    x, y, z, w = a["box"]
    u, v, s, t = b["box"]
    aw, ah = z - x, w - y
    bw, bh = s - u, t - v
    height_ok = (
        0.4 < bh / ah < 2.5
        if ah > 0 and aw > 0 and 0.8 < bw / aw < 1.25
        else ah > 0 and 0.65 < bh / ah < 1.55
    )
    if min(aw, ah, bw, bh) <= 0 or not (0.65 < bw / aw < 1.55 and height_ok):
        return None
    dx = abs((u + s - x - z) / 2)
    dy = abs((v + t - y - w) / 2)
    if dx > max(12, data["width"] * 0.035) or dy > min(
        data["height"] * 0.05, max(16, 2 * max(ah, bh))
    ):
        return None
    return dy + 0.7 * dx


def fill_instance_gaps(data, detections, policy, rules):
    """One-to-one tracks; an email elsewhere never conceals a missing instance."""
    populated = copy.deepcopy(detections)
    tracks = []
    pts = data["pts"]
    next_id = 0
    active = [effective(policy, rules, t) for t in pts]
    boundaries = {r[k] for r in rules for k in ("start", "end")}
    for i, items in enumerate(detections):
        tracks = [
            (last, hit, uid)
            for last, hit, uid in tracks
            if i - last <= 4 and pts[i] - pts[last] <= 0.2
        ]
        pairs = []
        for track, (last, hit, uid) in enumerate(tracks):
            gap = i - last
            if not 1 <= gap <= 4 or pts[i] - pts[last] > 0.2:
                continue
            if any(active[j] != active[last] for j in range(last, i + 1)):
                continue
            if any(pts[last] < t <= pts[i] for t in boundaries):
                continue
            for current, item in enumerate(items):
                distance = field_distance(hit, item, data)
                if distance is not None:
                    pairs.append((gap, distance, current, track))
        assignments = {}
        used = set()
        for _, _, current, track in sorted(pairs):
            if current not in assignments and track not in used:
                assignments[current] = track
                used.add(track)
        for current, item in enumerate(items):
            track = assignments.get(current)
            if track is None:
                populated[i][current]["track_key"] = f"a{next_id}"
                tracks.append((i, item, f"a{next_id}"))
                next_id += 1
                continue
            last, hit, uid = tracks[track]
            populated[i][current]["track_key"] = uid
            if item["type"] in active[i]:
                for j in range(last + 1, i):
                    mix = (pts[j] - pts[last]) / (pts[i] - pts[last])
                    box = [a + (b - a) * mix for a, b in zip(hit["box"], item["box"])]
                    populated[j].append(
                        {
                            "type": item["type"],
                            "box": box,
                            "confidence": 0,
                            "track_key": uid,
                            "reason": "short-instance-gap",
                            "anchors": [last, i],
                        }
                    )
            tracks[track] = (i, item, uid)
    return populated


def minimal_boxes(boxes):
    unique = sorted({tuple(b) for b in boxes if b[0] < b[2] and b[1] < b[3]})
    return [
        list(b) for b in unique if not any(b != a and contains(a, b) for a in unique)
    ]


def rendering_mode(policy, kind):
    return policy.get("type_modes", {}).get(kind, policy.get("mode", "gaussian"))


def local_fit(track, pos, pts, axis):
    """Centered weighted linear fit: constant-speed motion has no trailing lag."""
    i, _ = track[pos]
    near = []
    for j in range(max(0, pos - 12), min(len(track), pos + 13)):
        f, b = track[j]
        dt = pts[f] - pts[i]
        if abs(dt) <= 0.12:
            near.append((dt, math.exp(-0.5 * (dt / 0.06) ** 2), b[axis]))
    total = sum(w for _, w, _ in near)
    mt = sum(t * w for t, w, _ in near) / total
    mv = sum(w * v for _, w, v in near) / total
    variance = sum(w * (t - mt) ** 2 for t, w, _ in near)
    slope = (
        sum(w * (t - mt) * (v - mv) for t, w, v in near) / variance
        if variance > 1e-12
        else 0
    )
    return mv - slope * mt


def stabilize_track(track, pts, width, height, jitter):
    """Bounded stationary locks plus local motion fitting, always containing raw boxes."""
    output = {}
    locked = []
    n = len(track)
    if not track:
        return output, locked

    def bounds(items):
        return [
            min(b[0] for _, b in items),
            min(b[1] for _, b in items),
            max(b[2] for _, b in items),
            max(b[3] for _, b in items),
        ]

    typical_height = statistics.median(b[3] - b[1] for _, b in track)
    limits = [
        max(2 * jitter, min(12, typical_height * 0.2)),
        max(2 * jitter, min(28, typical_height * 0.4)),
    ]

    def stationary_bounds(items):
        return all(
            max(b[k] for _, b in items) - min(b[k] for _, b in items) <= limits[k % 2]
            for k in range(4)
        )

    if stationary_bounds(track):
        box = clamp(bounds(track), width, height, 2)
        return {i: box for i, _ in track}, [list(range(n))]
    moving = []
    for pos, (i, b) in enumerate(track):
        # Use elapsed time, not frame count, to handle VFR and slow accumulated drift.
        past = [
            v for f, v in track[max(0, pos - 60) : pos + 1] if pts[i] - pts[f] <= 0.5
        ]
        future = [v for f, v in track[pos : min(n, pos + 61)] if pts[f] - pts[i] <= 0.5]
        early = past[: max(1, len(past) // 2)]
        late = future[-max(1, len(future) // 2) :]
        changes = [
            statistics.median(v[k] for v in late)
            - statistics.median(v[k] for v in early)
            for k in range(4)
        ]
        # A glyph-height or width change is not a translation of the field.
        delta = max(
            (
                min(abs(changes[k]), abs(changes[k + 2]))
                if changes[k] * changes[k + 2] > 0
                else 0
            )
            for k in (0, 1)
        )
        moving.append(delta > max(2, jitter))
    for pos, (i, b) in enumerate(track):
        is_moving = any(
            moving[j] and abs(pts[track[j][0]] - pts[i]) <= 0.25
            for j in range(max(0, pos - 30), min(n, pos + 31))
        )
        if not is_moving:
            continue
        fitted = [local_fit(track, pos, pts, k) for k in range(4)]
        # A bad fit at an abrupt acceleration snaps to this frame rather than leaving a trail.
        if max(abs(a - c) for a, c in zip(fitted, b)) > 2 * jitter:
            fitted = list(b)
        margin = max(1, jitter)
        output[i] = clamp(
            [
                min(b[0], fitted[0] - margin),
                min(b[1], fitted[1] - margin),
                max(b[2], fitted[2] + margin),
                max(b[3], fitted[3] + margin),
            ],
            width,
            height,
        )
    pos = 0
    while pos < n:
        if track[pos][0] in output:
            pos += 1
            continue
        end = pos + 1
        while (
            end < n
            and track[end][0] not in output
            and stationary_bounds(track[pos : end + 1])
        ):
            end += 1
        group = track[pos:end]
        box = clamp(bounds(group), width, height, 2)
        for i, _ in group:
            output[i] = box
        locked.append(list(range(pos, end)))
        pos = end
    return output, locked


def plan_regions(data, detections, policy, rules):
    width, height = data["width"], data["height"]
    pts = data["pts"]
    populated = fill_instance_gaps(data, detections, policy, rules)
    raw = []
    fields = []
    previous_manual = []
    manual_id = 0
    boundaries = {r[k] for r in rules for k in ("start", "end")}
    for i, items in enumerate(populated):
        t = pts[i]
        active = effective(policy, rules, t)
        current = []
        manual = []
        for item in items:
            if item["type"] not in active:
                continue
            field = dict(
                item,
                box=clamp(item["box"], width, height, int(policy["padding"])),
                mode=rendering_mode(policy, item["type"]),
            )
            current.append(field)
        for entry in policy.get("manual_regions", []):
            if entry["start"] <= t < entry["end"] and entry["type"] in active:
                manual.append(
                    dict(
                        entry,
                        identity="manual",
                        box=clamp(entry["box"], width, height),
                        mode=rendering_mode(policy, entry["type"]),
                    )
                )
        pairs = []
        for m, field in enumerate(manual):
            for p, old in enumerate(previous_manual):
                if (
                    field["mode"] != old["mode"]
                    or i == 0
                    or any(pts[i - 1] < b <= t for b in boundaries)
                ):
                    continue
                distance = field_distance(old, field, data)
                if distance is not None:
                    pairs.append((distance, m, p))
        assigned = {}
        used = set()
        for _, m, p in sorted(pairs):
            if m not in assigned and p not in used:
                assigned[m] = p
                used.add(p)
        for m, field in enumerate(manual):
            if m in assigned:
                field["track_key"] = previous_manual[assigned[m]]["track_key"]
            else:
                field["track_key"] = f"m{manual_id}"
                manual_id += 1
        previous_manual = manual
        current.extend(manual)
        # A containing mask dominates only when its rendering mode is the same.
        filtered = []
        for index, item in enumerate(current):
            if any(
                index != j
                and item["mode"] == other["mode"]
                and contains(other["box"], item["box"])
                and (other["box"] != item["box"] or j < index)
                for j, other in enumerate(current)
            ):
                continue
            filtered.append(item)
        fields.append(filtered)
        raw.append(minimal_boxes([f["box"] for f in current]))
    tracks = {}
    for i, items in enumerate(fields):
        for item in items:
            tracks.setdefault((item["track_key"], item["mode"]), []).append(
                (i, item["box"])
            )
    layers = {mode: [[] for _ in pts] for mode in ("gaussian", "solid")}
    stats = {
        "tracks": len(tracks),
        "locked_frames": 0,
        "raw_locked_edge_change": 0,
        "output_locked_edge_change": 0,
    }
    for (_, mode), observations in tracks.items():
        # Containment deduplication can hide a track; never smooth across those gaps.
        segments = []
        for observation in observations:
            i, _ = observation
            if (
                not segments
                or i != segments[-1][-1][0] + 1
                or any(pts[segments[-1][-1][0]] < b <= pts[i] for b in boundaries)
            ):
                segments.append([])
            segments[-1].append(observation)
        for track in segments:
            if policy.get("smooth", True):
                stable, locked = stabilize_track(
                    track, pts, width, height, float(policy.get("jitter_pixels", 4))
                )
            else:
                stable = {i: b for i, b in track}
                locked = []
            for i, box in stable.items():
                layers[mode][i].append(box)
            for group in locked:
                stats["locked_frames"] += len(group)
                for a, b in zip(group, group[1:]):
                    ia, ba = track[a]
                    ib, bb = track[b]
                    stats["raw_locked_edge_change"] += sum(
                        abs(x - y) for x, y in zip(ba, bb)
                    )
                    stats["output_locked_edge_change"] += sum(
                        abs(x - y) for x, y in zip(stable[ia], stable[ib])
                    )
    smooth = [
        minimal_boxes(layers["gaussian"][i] + layers["solid"][i])
        for i in range(len(pts))
    ]
    for required, covered in zip(raw, smooth):
        if not all(any(contains(a, b) for a in covered) for b in required):
            fail("Stabilization removed required coverage")
    return raw, smooth, layers, stats


def make_regions(data, detections, policy, rules):
    raw, smooth, _, _ = plan_regions(data, detections, policy, rules)
    return raw, smooth


def next_revision(root):
    existing = [int(p.stem[1:]) for p in (root / "revisions").glob("r[0-9]*.json")]
    return f"r{max(existing,default=0)+1:04d}"


def load_revision(root, name=None):
    name = name or read(root / "project.json").get("latest_revision")
    if not name or not re.fullmatch(r"r\d{4,}", name):
        fail("Specify an existing revision such as r0001")
    revision = read(root / "revisions" / f"{name}.json")
    data = read(root / "project.json")
    if revision["source_sha256"] != data["source_sha256"]:
        fail("Revision belongs to a different source video")
    if len(revision["regions"]) != len(data["pts"]) or len(
        revision["required_regions"]
    ) != len(data["pts"]):
        fail("Revision frame count differs from source")
    return revision


def save_revision(
    root, data, policy, rules, detections, parent=None, detection_file=None
):
    required, regions, layers, stabilization = plan_regions(
        data, detections, policy, rules
    )
    findings = audit_source_frames(
        data, list(records(root / "cache" / "scan.jsonl")), policy, rules, required
    )
    name = next_revision(root)
    value = {
        "revision": name,
        "parent": parent,
        "source_sha256": data["source_sha256"],
        "policy": policy,
        "rules": rules,
        "detection_file": detection_file,
        "planner_version": PLANNER_VERSION,
        "review_findings": findings,
        "required_regions": required,
        "regions": regions,
        "layers": layers,
        "stabilization": stabilization,
    }
    write(root / "revisions" / f"{name}.json", value)
    data["latest_revision"] = name
    write(root / "project.json", data)
    print(
        json.dumps(
            {
                "revision": name,
                "active_frames": sum(bool(r) for r in regions),
                "regions": sum(map(len, regions)),
                "unresolved_source_candidates": len(findings),
            }
        )
    )
    return value


def commands(data, regions, destination, prefix="r"):
    slots = max(1, max(map(len, regions), default=0))
    if slots > 80:
        fail("More than 80 simultaneous regions; inspect detections before rendering")
    previous = None
    lines = []
    for t, boxes in zip(data["pts"], regions):
        state = [(b[0], b[1], b[2] - b[0], b[3] - b[1], "white") for b in boxes]
        state.extend([(0, 0, 1, 1, "black@0")] * (slots - len(state)))
        updates = []
        for i, current in enumerate(state):
            if previous is not None and current == previous[i]:
                continue
            updates.extend(
                f"drawbox@{prefix}{i} {key} {v}"
                for key, v in zip(("x", "y", "w", "h", "color"), current)
            )
        if updates:
            lines.append(f"{max(0,t-0.000001):.9f} " + ", ".join(updates) + ";")
        previous = state
    destination.write_text("\n".join(lines) + "\n")
    filters = [
        f"drawbox@{prefix}{i}=x=0:y=0:w=1:h=1:color=black@0:t=fill"
        for i in range(slots)
    ]
    return ",".join(filters)


def graph(data, revision, command_path, mask_only=False):
    if not mask_only and any(revision.get("layers", {}).get("solid", [])):
        return mixed_graph(data, revision, command_path)
    draws = commands(data, revision["regions"], command_path)
    # drawbox may encode white as limited-range 235. Alpha must be exactly 255;
    # otherwise a faint, OCR-readable copy of the original text leaks through.
    mask = f"format=gray,lut=y=0,sendcmd=f='{command_path.name}',{draws},lut=y='if(gte(val,128),255,0)',setparams=range=full"
    if mask_only:
        return f"[0:v]{mask}[m]"
    feather = float(revision["policy"]["feather"])
    # A max merge keeps the entire core opaque; feathering only expands outward.
    return (
        f'[0:v]split=3[base][copy][seed];[copy]gblur=sigma={float(revision["policy"]["sigma"])}:steps=3[blur];'
        f"[seed]{mask},split=2[core][edge];[edge]gblur=sigma={feather}:steps=2[soft];"
        "[core][soft]blend=all_mode=lighten,format=gray,split=2[alpha][audit];"
        "[blur][alpha]alphamerge[cover];[base][cover]overlay=shortest=1:format=auto,format=yuv420p[v]"
    )


def mixed_graph(data, revision, command_path):
    filters = [
        "[0:v]split=4[base][copy][gseed][sseed]",
        f'[copy]gblur=sigma={float(revision["policy"]["sigma"])}:steps=3[blur]',
    ]
    for mode, prefix in [("gaussian", "g"), ("solid", "s")]:
        path = command_path.with_name(command_path.stem + "-" + mode + ".txt")
        draws = commands(data, revision["layers"][mode], path, prefix)
        mask = f"format=gray,lut=y=0,sendcmd=f='{path.name}',{draws},lut=y='if(gte(val,128),255,0)',setparams=range=full"
        filters.extend(
            [
                f"[{prefix}seed]{mask},split=3[{prefix}core][{prefix}edge][{prefix}audit]",
                f'[{prefix}edge]gblur=sigma={float(revision["policy"]["feather"])}:steps=2[{prefix}soft]',
                f"[{prefix}core][{prefix}soft]blend=all_mode=lighten,format=gray[{prefix}alpha]",
            ]
        )
    filters.extend(
        [
            "[gaudit][saudit]blend=all_mode=lighten,format=gray[audit]",
            "[blur][galpha]alphamerge[gcover]",
            "[base][gcover]overlay=shortest=1:format=auto,split=2[vbase][blackseed]",
            "[blackseed]lutrgb=r=0:g=0:b=0[black]",
            "[black][salpha]alphamerge[scover]",
            "[vbase][scover]overlay=shortest=1:format=auto,format=yuv420p[v]",
        ]
    )
    return ";".join(filters)


def environment_report(full=False):
    report = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "python_path": sys.executable,
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "swiftc": shutil.which("swiftc"),
        "issues": [],
        "full_check": False,
    }

    def check(name, command, timeout=20, cwd=None):
        try:
            result = subprocess.run(
                [str(x) for x in command],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
            if result.returncode:
                report["issues"].append(
                    {
                        "tool": name,
                        "error": (result.stderr or result.stdout)[-1200:]
                        or "Command failed",
                    }
                )
                return False
            return True
        except (OSError, subprocess.TimeoutExpired) as error:
            report["issues"].append({"tool": name, "error": str(error)})
            return False

    if sys.platform != "darwin":
        report["issues"].append(
            {
                "tool": "platform",
                "error": "Apple Vision OCR requires macOS; no cloud fallback is used",
            }
        )
    if sys.version_info < (3, 9):
        report["issues"].append(
            {"tool": "python", "error": "Python 3.9 or newer is required"}
        )
    for name in ("ffmpeg", "ffprobe"):
        if not report[name]:
            report["issues"].append({"tool": name, "error": "Missing executable"})
        else:
            check(name, [report[name], "-version"])
    if sys.platform == "darwin":
        # Avoid triggering the macOS launcher installation dialog during a check.
        tools = check("developer_tools", ["xcode-select", "-p"])
        if tools:
            tools = check("developer_tools", ["xcrun", "--find", "swiftc"])
        if tools and report["swiftc"]:
            check("swiftc", [report["swiftc"], "--version"])
        elif tools:
            report["issues"].append({"tool": "swiftc", "error": "Missing executable"})
    if full and not report["issues"]:
        with tempfile.TemporaryDirectory(prefix="video-privacy-doctor-") as folder:
            root = Path(folder)
            vision = root / "vision"
            source = root / "source.mp4"
            output = root / "output.mp4"
            audit = root / "mask.mkv"
            scan = root / "scan.jsonl"
            # Compile the actual helper against the selected SDK.
            compiled = check(
                "vision_compile",
                [report["swiftc"], "-O", HERE / "vision_ocr.swift", "-o", vision],
                120,
            )
            generated = check(
                "ffmpeg_encode",
                [
                    report["ffmpeg"],
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=white:s=128x128:r=10:d=0.2",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    source,
                ],
            )
            if compiled and generated:
                ocr_ok = check("vision_ocr", [vision, source, scan, "0"], 60)
                if ocr_ok:
                    try:
                        rows = list(records(scan))
                        if len(rows) != 2 or any(
                            (r["width"], r["height"]) != (128, 128) for r in rows
                        ):
                            raise ValueError(
                                "Unexpected OCR frame count or orientation"
                            )
                    except (OSError, ValueError, KeyError) as error:
                        report["issues"].append(
                            {"tool": "vision_ocr", "error": str(error)}
                        )
                filter_text = graph(
                    {"pts": [0, 0.1]},
                    {
                        "regions": [[[8, 8, 40, 40]], [[12, 12, 44, 44]]],
                        "policy": DEFAULT_POLICY,
                    },
                    root / "commands.txt",
                )
                rendered = check(
                    "ffmpeg_render",
                    [
                        report["ffmpeg"],
                        "-v",
                        "error",
                        "-copyts",
                        "-start_at_zero",
                        "-i",
                        source,
                        "-filter_complex",
                        filter_text,
                        "-map",
                        "[v]",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-fps_mode",
                        "passthrough",
                        "-enc_time_base",
                        "1/1000000",
                        output,
                        "-map",
                        "[audit]",
                        "-c:v",
                        "ffv1",
                        "-level",
                        "3",
                        "-fps_mode",
                        "passthrough",
                        audit,
                    ],
                    60,
                    cwd=root,
                )
                if rendered:
                    check(
                        "ffprobe_read",
                        [
                            report["ffprobe"],
                            "-v",
                            "error",
                            "-show_frames",
                            "-of",
                            "json",
                            output,
                        ],
                    )
            report["full_check"] = True
    report["ready"] = not report["issues"]
    return report


def cmd_doctor(args):
    report = environment_report(getattr(args, "full", False))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 2


def cmd_init(args):
    source = Path(args.input).expanduser().resolve()
    root = paths(args.project)
    if not source.is_file():
        fail("Input video does not exist")
    if (root / "project.json").exists():
        fail("Project already exists; select it rather than initializing again")
    info = probe(source)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        fail("Input has no video stream")
    if video.get("color_transfer") in {"smpte2084", "arib-std-b67"}:
        fail(
            "HDR needs an explicit color-management decision; this version does not silently convert HDR"
        )
    rotation = float(video.get("tags", {}).get("rotate", 0))
    for side in video.get("side_data_list", []):
        rotation = float(side.get("rotation", rotation))
    width, height = int(video["width"]), int(video["height"])
    if abs(rotation) % 180 == 90:
        width, height = height, width
    if width % 2 or height % 2:
        fail(
            "Odd display dimensions need an explicit crop/pad decision before H.264 export"
        )
    start = float(info["format"].get("start_time", 0))
    pts = timeline(source, start)
    data = {
        "schema": 1,
        "source": str(source),
        "source_size": source.stat().st_size,
        "source_sha256": digest(source),
        "width": width,
        "height": height,
        "rotation": rotation,
        "source_start_time": start,
        "pts": pts,
        "duration": float(info["format"]["duration"]),
        "audio_codecs": [
            s["codec_name"] for s in info["streams"] if s["codec_type"] == "audio"
        ],
        "audio_starts": [
            float(s.get("start_time", start)) - start
            for s in info["streams"]
            if s["codec_type"] == "audio"
        ],
    }
    root.mkdir(parents=True, exist_ok=True)
    write(root / "project.json", data)
    write(root / "policy.json", DEFAULT_POLICY)
    print(
        json.dumps(
            {
                "project": str(root),
                "frames": len(pts),
                "duration": data["duration"],
                "display": [width, height],
            }
        )
    )


def cmd_scan(args):
    if not math.isfinite(args.chunk_seconds) or args.chunk_seconds <= 0:
        fail("Chunk seconds must be finite and positive")
    root, data = project(args.project)
    destination = root / "cache" / "scan.jsonl"
    meta_path = root / "cache" / "scan-meta.json"
    policy = read(root / "policy.json")
    languages = policy.get("ocr_languages", DEFAULT_POLICY["ocr_languages"])
    identity = {
        "source_sha256": data["source_sha256"],
        "helper_sha256": digest(HERE / "vision_ocr.swift"),
        "languages": languages,
    }
    stamp = read(meta_path) if meta_path.exists() else {}
    if (
        destination.exists()
        and stamp.get("identity") == identity
        and stamp.get("scan_sha256") == digest(destination)
        and not args.force
    ):
        rows = align_scan(data, list(records(destination)), data["source_start_time"])
        check_scan(data, rows)
        with destination.open("w") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        print("Using complete local OCR cache")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    scan_video(
        root,
        data["source"],
        destination,
        data=data,
        languages=languages,
        chunk_seconds=args.chunk_seconds,
        force=args.force,
    )
    check_scan(data, list(records(destination)))
    write(meta_path, {"identity": identity, "scan_sha256": digest(destination)})
    print("Complete OCR cache validated against source frames and orientation")


def validate_policy(policy, data):
    if set(policy["types"]) - TYPES:
        fail("Policy contains an unknown privacy type")
    if not 6 <= float(policy["sigma"]) <= 100:
        fail("Gaussian sigma must be between 6 and 100 pixels")
    if (
        not 0 <= float(policy["feather"]) <= 12
        or not 0 <= int(policy["padding"]) <= 100
    ):
        fail("Invalid padding or feather size")
    if policy.get("mode", "gaussian") not in {"gaussian", "solid"}:
        fail("Unknown rendering mode")
    if set(policy.get("type_modes", {})) - TYPES or any(
        mode not in {"gaussian", "solid"}
        for mode in policy.get("type_modes", {}).values()
    ):
        fail("Invalid per-type rendering modes")
    if not 0 <= float(policy.get("jitter_pixels", 4)) <= 20:
        fail("Jitter tolerance must be between 0 and 20 pixels")
    languages = policy.get("ocr_languages", DEFAULT_POLICY["ocr_languages"])
    if (
        not isinstance(languages, list)
        or not languages
        or not all(
            isinstance(x, str) and re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*", x)
            for x in languages
        )
    ):
        fail("Invalid OCR language list")
    for r in policy.get("manual_regions", []) + policy.get("review_exclusions", []):
        interval(r["start"], r["end"], data["duration"])
        if r["type"] not in TYPES or len(r["box"]) != 4:
            fail("Invalid manual region")
        a, b, c, d = r["box"]
        if not (0 <= a < c <= data["width"] and 0 <= b < d <= data["height"]):
            fail("Manual region is outside display coordinates")
    for r in policy.get("review_exclusions", []):
        if not str(r.get("reason", "")).strip():
            fail("A false-positive review exclusion needs a reason")


def cmd_plan(args):
    root, data = project(args.project)
    scan = list(records(root / "cache" / "scan.jsonl"))
    check_scan(data, scan)
    policy = copy.deepcopy(DEFAULT_POLICY)
    policy.update(read(args.policy or root / "policy.json"))
    validate_policy(policy, data)
    hints = hints_for(scan, policy)
    detections = [detect_frame(frame, policy, hints) for frame in scan]
    key = hashlib.sha256(
        (
            str(PLANNER_VERSION)
            + json.dumps(policy, sort_keys=True)
            + digest(root / "cache" / "scan.jsonl")
        ).encode()
    ).hexdigest()[:16]
    detection_file = f"cache/detections-{key}.json"
    write(
        root / detection_file,
        {
            "source_sha256": data["source_sha256"],
            "planner_version": PLANNER_VERSION,
            "frames": detections,
            "hints": {k: sorted(v) for k, v in hints.items()},
            "review_required": True,
            "low_confidence_regions": sum(
                v["confidence"] < 0.5 for f in detections for v in f
            ),
        },
    )
    save_revision(root, data, policy, [], detections, detection_file=detection_file)


def cmd_revise(args):
    root, data = project(args.project)
    base = load_revision(root, args.base)
    policy = copy.deepcopy(base["policy"])
    rules = copy.deepcopy(base["rules"])
    a, b = interval(args.start, args.end, data["duration"])
    if args.add_box:
        box = [float(x) for x in args.add_box.split(",")]
        policy.setdefault("manual_regions", []).append(
            {"start": a, "end": b, "type": args.type, "box": box}
        )
    else:
        action = (
            "set"
            if args.set is not None
            else (
                "keep"
                if args.keep is not None
                else "remove" if args.remove is not None else "set"
            )
        )
        values = (
            args.set
            if args.set is not None
            else (
                args.keep
                if args.keep is not None
                else args.remove if args.remove is not None else ""
            )
        )
        rules.append(
            {"start": a, "end": b, "action": action, "types": sorted(selected(values))}
        )
    validate_policy(policy, data)
    detections = read(root / base["detection_file"])["frames"]
    save_revision(
        root, data, policy, rules, detections, base["revision"], base["detection_file"]
    )


def cmd_render(args):
    root, data = project(args.project)
    revision = load_revision(root, args.revision)
    name = revision["revision"]
    findings = audit_source_frames(
        data,
        list(records(root / "cache" / "scan.jsonl")),
        revision["policy"],
        revision["rules"],
        revision["required_regions"],
    )
    write(
        root / "reports" / f"{name}-planning.json",
        {
            "revision": name,
            "source_frames_checked": len(data["pts"]),
            "source_findings": findings,
        },
    )
    if findings:
        fail(
            f"Uncovered source candidates: {len(findings)}; inspect reports/{name}-planning.json, add field boxes or localized false-positive review_exclusions, then revise or re-plan"
        )
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else root / "exports" / f"{name}-gaussian.mp4"
    )
    if output.exists():
        fail("Output exists; choose a new filename or revision")
    if output == Path(data["source"]):
        fail("Never overwrite the source video")
    output.parent.mkdir(parents=True, exist_ok=True)
    cache = root / "cache"
    cache.mkdir(exist_ok=True)
    command_path = cache / f"{name}-commands.txt"
    filter_path = cache / f"{name}-filter.txt"
    filter_path.write_text(graph(data, revision, command_path))
    audit = cache / f"{name}-mask.mkv"
    audio = "copy" if all(c == "aac" for c in data["audio_codecs"]) else "aac"
    # Both outputs derive from the source frames, retaining VFR presentation times.
    run(
        [
            binary("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-copyts",
            "-start_at_zero",
            "-i",
            data["source"],
            "-filter_complex",
            filter_path.read_text(),
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "19",
            "-fps_mode",
            "passthrough",
            "-enc_time_base",
            "1/1000000",
            "-c:a",
            audio,
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            output,
            "-map",
            "[audit]",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-fps_mode",
            "passthrough",
            "-enc_time_base",
            "1/1000000",
            audit,
        ],
        cwd=cache,
    )
    write(
        root / "renders" / f"{name}.json",
        {
            "revision": name,
            "source_sha256": data["source_sha256"],
            "output": str(output),
            "output_sha256": digest(output),
            "mask": str(audit),
            "mask_sha256": digest(audit),
        },
    )
    print(json.dumps({"revision": name, "output": str(output)}, ensure_ascii=False))


def cmd_verify(args):
    if not math.isfinite(args.sample_seconds) or args.sample_seconds < 0:
        fail("OCR sampling seconds must be finite and nonnegative")
    root, data = project(args.project)
    revision = load_revision(root, args.revision)
    name = revision["revision"]
    rendered = read(root / "renders" / f"{name}.json")
    output = Path(rendered["output"])
    if (
        digest(output) != rendered["output_sha256"]
        or digest(rendered["mask"]) != rendered["mask_sha256"]
    ):
        fail("Rendered output or audit mask changed")
    meta = probe(output)
    video = next(s for s in meta["streams"] if s["codec_type"] == "video")
    out_pts = timeline(output, float(meta["format"].get("start_time", 0)))
    timing_ok = len(out_pts) == len(data["pts"]) and all(
        abs(a - b) < 0.002 for a, b in zip(data["pts"], out_pts)
    )
    dimensions_ok = (video["width"], video["height"]) == (data["width"], data["height"])
    audio_count = sum(s["codec_type"] == "audio" for s in meta["streams"])
    audio_starts = [
        float(s.get("start_time", 0)) - float(meta["format"].get("start_time", 0))
        for s in meta["streams"]
        if s["codec_type"] == "audio"
    ]
    audio_sync_ok = len(audio_starts) == len(data["audio_starts"]) and all(
        abs(a - b) < 0.05 for a, b in zip(audio_starts, data["audio_starts"])
    )
    process = subprocess.Popen(
        [
            binary("ffmpeg"),
            "-v",
            "error",
            "-i",
            rendered["mask"],
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-fps_mode",
            "passthrough",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
    )
    W, H = data["width"], data["height"]
    covered = 0
    failures = []
    frames = 0
    for index, boxes in enumerate(revision["required_regions"]):
        image = process.stdout.read(W * H)
        if len(image) != W * H:
            fail("Audit mask ended early")
        for x0, y0, x1, y1 in boxes:
            covered += 1
            white = b"\xff" * (x1 - x0)
            if any(image[y * W + x0 : y * W + x1] != white for y in range(y0, y1)):
                failures.append(index)
        frames += 1
    extra = process.stdout.read(1)
    code = process.wait()
    if extra or code:
        fail("Audit mask has extra frames or failed to decode")
    original = list(records(root / "cache" / "scan.jsonl"))
    source_findings = audit_source_frames(
        data,
        original,
        revision["policy"],
        revision["rules"],
        revision["required_regions"],
    )
    report = {
        "revision": name,
        "frames": frames,
        "required_regions": covered,
        "uncovered_regions": len(failures),
        "timing_ok": timing_ok,
        "dimensions_ok": dimensions_ok,
        "audio_streams_ok": audio_count == len(data["audio_codecs"]),
        "audio_sync_ok": audio_sync_ok,
        "source_audit_frames": len(original),
        "source_findings": source_findings,
        "ocr_checked": False,
        "ocr_complete": False,
        "ocr_frames_checked": 0,
        "ocr_sample_seconds": None,
        "ocr_findings": [],
        "visual_review_still_required": True,
        "limits": "Core coverage applies to planned regions. Even full-frame OCR can misread or miss privacy; visual review is still required.",
    }
    if not args.skip_ocr:
        destination = root / "cache" / f"{name}-output-scan.jsonl"
        scan_video(
            root,
            str(output),
            destination,
            float(args.sample_seconds),
            data,
            float(meta["format"].get("start_time", 0)),
            revision["policy"].get("ocr_languages"),
        )
        hints = hints_for(original, revision["policy"])
        for frame in records(destination):
            i = frame["frame"]
            if i >= len(data["pts"]):
                fail("Output OCR has unexpected frame index")
            active = effective(revision["policy"], revision["rules"], data["pts"][i])
            strong = detect_frame(frame, revision["policy"], hints)
            for hit in strong + review_candidates(frame, hints, strong):
                if hit["type"] in active and not review_excluded(
                    hit, revision["policy"], data["pts"][i]
                ):
                    report["ocr_findings"].append(
                        {
                            "frame": i,
                            "time": data["pts"][i],
                            "type": hit["type"],
                            "box": hit["box"],
                            "reason": hit["reason"],
                        }
                    )
            report["ocr_frames_checked"] += 1
        report["ocr_checked"] = True
        report["ocr_sample_seconds"] = float(args.sample_seconds)
        report["ocr_complete"] = args.sample_seconds == 0 and report[
            "ocr_frames_checked"
        ] == len(data["pts"])
    write(root / "reports" / f"{name}.json", report)
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in {"ocr_findings", "source_findings"}
            },
            ensure_ascii=False,
        )
    )
    print("Uncovered source candidates:", len(source_findings))
    print("OCR findings:", len(report["ocr_findings"]))
    if (
        failures
        or not timing_ok
        or not dimensions_ok
        or not audio_sync_ok
        or audio_count != len(data["audio_codecs"])
        or source_findings
        or report["ocr_findings"]
    ):
        return 2


def cmd_inspect(args):
    root, data = project(args.project)
    revision = load_revision(root, args.revision) if not args.source else None
    source = (
        read(root / "renders" / f'{revision["revision"]}.json')["output"]
        if revision
        else data["source"]
    )
    folder = root / "review" / (revision["revision"] if revision else "source")
    folder.mkdir(parents=True, exist_ok=True)
    for value in args.at.split(","):
        t = timestamp(value, data["duration"])
        if not 0 <= t < data["duration"]:
            fail("Review timestamp is outside video")
        output = folder / f"{t:010.3f}.png"
        run(
            [
                binary("ffmpeg"),
                "-v",
                "error",
                "-y",
                "-ss",
                str(t),
                "-i",
                source,
                "-frames:v",
                "1",
                output,
            ]
        )
        print(str(output))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("doctor")
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("init")
    p.add_argument("--input", required=True)
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("scan")
    p.add_argument("--project", required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--chunk-seconds", type=float, default=30)
    p.set_defaults(func=cmd_scan)
    p = sub.add_parser("plan")
    p.add_argument("--project", required=True)
    p.add_argument("--policy")
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("revise")
    p.add_argument("--project", required=True)
    p.add_argument("--base")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    modes = p.add_mutually_exclusive_group(required=True)
    for name in ["keep", "remove", "set", "add-box"]:
        modes.add_argument("--" + name)
    modes.add_argument("--clear", action="store_true")
    p.add_argument("--type", default="custom", choices=sorted(TYPES))
    p.set_defaults(func=cmd_revise)
    p = sub.add_parser("render")
    p.add_argument("--project", required=True)
    p.add_argument("--revision")
    p.add_argument("--output")
    p.set_defaults(func=cmd_render)
    p = sub.add_parser("verify")
    p.add_argument("--project", required=True)
    p.add_argument("--revision")
    p.add_argument("--skip-ocr", action="store_true")
    p.add_argument(
        "--sample-seconds",
        type=float,
        default=0,
        help="0: every frame (default); positive: partial quick check only",
    )
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("inspect")
    p.add_argument("--project", required=True)
    p.add_argument("--revision")
    p.add_argument("--source", action="store_true")
    p.add_argument("--at", required=True)
    p.set_defaults(func=cmd_inspect)
    args = parser.parse_args()
    try:
        return args.func(args) or 0
    except (RuntimeError, FileNotFoundError, ValueError, KeyError) as error:
        print("Error:", str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
