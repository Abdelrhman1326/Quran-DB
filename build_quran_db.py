#!/usr/bin/env python3
"""
Build a verified Quran SQLite database from two independent sources.

  Source A (primary text + structure): Tanzil Uthmani text + quran-data.xml
  Source B (cross-check only):         Quran.com API v4 (text_uthmani)

Nothing is written unless ALL hard checks pass:
  * 114 surahs, per-surah ayah counts match the canonical table, 6,236 ayahs
  * bismillah removed from ayah 1 of every surah except 1 (it IS 1:1) and 9
  * 30 juz / 60 hizb / 240 quarters / 7 manzils / 556 rukus / 604 pages / 15 sajdas
  * every ayah's consonantal skeleton in A equals the skeleton in B
    (any difference is listed in <out>/crosscheck_report.txt; use --lenient
     to build anyway after you have reviewed the report)

Usage (needs internet, Python 3.9+, no extra packages):
  python3 build_quran_db.py --out quran-data

Offline / manual download:
  python3 build_quran_db.py --tanzil-file quran-uthmani.txt \
          --xml-file quran-data.xml --qcom-dir ./qcom_chapters
  (Tanzil text: https://tanzil.net/download/  -> Uthmani, "Text (with aya numbers)"
   i.e. lines of  sura|aya|text.   qcom_chapters/1.json ... 114.json are the
   responses of https://api.quran.com/api/v4/quran/verses/uthmani?chapter_number=N)

Licences: Tanzil text is CC BY 3.0 (keep the attribution, do not alter the text
itself); Quran.com data is used for verification only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

TANZIL_TEXT_URL = (
    "https://tanzil.net/pub/download/index.php"
    "?quranType=uthmani&outType=txt-2&agree=true"
)
TANZIL_XML_URL = "https://tanzil.net/res/text/metadata/quran-data.xml"
QCOM_URL = "https://api.quran.com/api/v4/quran/verses/uthmani?chapter_number={n}"

# Canonical ayah counts (Hafs, Kufan numbering - the Madinah mushaf).
AYAH_COUNTS = [
    7, 286, 200, 176, 120, 165, 206, 75, 129, 109, 123, 111, 43, 52, 99, 128, 111, 110,
    98, 135, 112, 78, 118, 64, 77, 227, 93, 88, 69, 60, 34, 30, 73, 54, 45, 83, 182, 88,
    75, 85, 54, 53, 89, 59, 37, 35, 38, 29, 18, 45, 60, 49, 62, 55, 78, 96, 29, 22, 24,
    13, 14, 11, 11, 18, 12, 12, 30, 52, 52, 44, 28, 28, 20, 56, 40, 31, 50, 40, 46, 42,
    29, 19, 36, 25, 22, 17, 19, 26, 30, 20, 15, 21, 11, 8, 8, 19, 5, 8, 8, 11, 11, 8, 3,
    9, 5, 4, 7, 3, 6, 3, 5, 4, 5, 6,
]
assert len(AYAH_COUNTS) == 114 and sum(AYAH_COUNTS) == 6236

EXPECTED = {"juz": 30, "quarter": 240, "manzil": 7, "ruku": 556, "page": 604, "sajda": 15}


# ----------------------------------------------------------------- helpers
def fetch(url: str, retries: int = 4) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "quran-db-builder/2.0"})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"download failed: {url}: {last}")


def skeleton(s: str) -> str:
    """Consonantal skeleton: drop all marks/tatweel/small signs, fold letter variants."""
    out = []
    for ch in s:
        cp = ord(ch)
        if ch.isspace() or ch == "ـ":
            continue
        if unicodedata.category(ch) in ("Mn", "Me", "Mc") or 0x06D6 <= cp <= 0x06ED:
            continue
        if ch in "ٱأإآ":
            ch = "ا"           # alef wasla / hamza-alef / madda -> alef
        elif ch in "ىئ":
            ch = "ي"           # alef maksura / hamza-on-yeh -> yeh
        elif ch == "ؤ":
            ch = "و"           # hamza-on-waw -> waw
        elif ch == "ء":
            continue                # bare hamza placement differs by edition
        out.append(ch)
    return "".join(out)


BISMILLAH_SKEL = skeleton("بسم الله الرحمن الرحيم")


def strip_bismillah(text: str) -> tuple[str, bool]:
    """Remove a leading bismillah (first 4 words) if present. Works for any spelling
    variant of it because matching is done on the consonantal skeleton."""
    words = text.split(" ")
    if len(words) > 4 and skeleton(" ".join(words[:4])) == BISMILLAH_SKEL:
        return " ".join(words[4:]), True
    return text, False


# ----------------------------------------------------------------- parsing
def parse_tanzil_text(raw: str) -> dict[tuple[int, int], str]:
    verses: dict[tuple[int, int], str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            raise RuntimeError(f"Unexpected Tanzil line: {line[:80]!r}")
        s, a, t = int(parts[0]), int(parts[1]), parts[2].strip()
        if (s, a) in verses:
            raise RuntimeError(f"Duplicate verse {s}:{a} in Tanzil text")
        verses[(s, a)] = t
    return verses


def parse_tanzil_xml(raw: bytes) -> dict:
    root = ET.fromstring(raw)
    meta: dict = {"suras": {}, "juz": [], "quarter": [], "manzil": [], "ruku": [],
                  "page": [], "sajda": []}
    for el in root.iter("sura"):
        meta["suras"][int(el.get("index"))] = {
            "name_arabic": el.get("name"),
            "name_transliteration": el.get("tname"),
            "name_english": el.get("ename"),
            "revelation_type": el.get("type"),
            "revelation_order": int(el.get("order")) if el.get("order") else None,
            "ayas": int(el.get("ayas")),
        }
    for tag in ("juz", "quarter", "manzil", "ruku", "page"):
        for el in root.iter(tag):
            meta[tag].append((int(el.get("index")), int(el.get("sura")), int(el.get("aya"))))
        meta[tag].sort()
    for el in root.iter("sajda"):
        meta["sajda"].append(
            (int(el.get("index")), int(el.get("sura")), int(el.get("aya")), el.get("type"))
        )
    meta["sajda"].sort()
    return meta


def parse_qcom(chapter_json: dict) -> dict[tuple[int, int], str]:
    out = {}
    for v in chapter_json["verses"]:
        s, a = v["verse_key"].split(":")
        out[(int(s), int(a))] = v["text_uthmani"]
    return out


def marker_lookup(markers: list[tuple[int, int, int]]):
    """markers: sorted (index, sura, aya) start points. Returns f(sura, aya)->index."""
    pts = [((s, a), i) for i, s, a in markers]

    def f(s: int, a: int) -> int:
        cur = None
        for pos, idx in pts:
            if pos <= (s, a):
                cur = idx
            else:
                break
        return cur  # type: ignore[return-value]

    return f


# ----------------------------------------------------------------- main build
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", default="quran-data")
    ap.add_argument("--tanzil-file")
    ap.add_argument("--xml-file")
    ap.add_argument("--qcom-dir")
    ap.add_argument("--lenient", action="store_true",
                    help="build even if the cross-check against Quran.com reports differences")
    args = ap.parse_args()

    out = Path(args.out).expanduser().resolve()
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # ---- Source A
    if args.tanzil_file:
        a_raw = Path(args.tanzil_file).read_bytes()
    else:
        print("Downloading Tanzil Uthmani text ...")
        a_raw = fetch(TANZIL_TEXT_URL)
    (raw_dir / "tanzil-quran-uthmani.txt").write_bytes(a_raw)
    tanzil = parse_tanzil_text(a_raw.decode("utf-8"))

    if args.xml_file:
        x_raw = Path(args.xml_file).read_bytes()
    else:
        print("Downloading Tanzil metadata ...")
        x_raw = fetch(TANZIL_XML_URL)
    (raw_dir / "quran-data.xml").write_bytes(x_raw)
    meta = parse_tanzil_xml(x_raw)

    # ---- Source B
    qcom: dict[tuple[int, int], str] = {}
    for n in range(1, 115):
        if args.qcom_dir:
            data = json.loads((Path(args.qcom_dir) / f"{n}.json").read_text("utf-8"))
        else:
            if n == 1:
                print("Downloading Quran.com cross-check text (114 requests) ...")
            data = json.loads(fetch(QCOM_URL.format(n=n)).decode("utf-8"))
            (raw_dir / f"qcom-{n}.json").write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        qcom.update(parse_qcom(data))

    # ---- Structural checks (hard)
    errors: list[str] = []
    for s in range(1, 115):
        n = sum(1 for (ss, _a) in tanzil if ss == s)
        if n != AYAH_COUNTS[s - 1]:
            errors.append(f"Tanzil surah {s}: {n} ayahs, expected {AYAH_COUNTS[s - 1]}")
        if meta["suras"].get(s, {}).get("ayas") != AYAH_COUNTS[s - 1]:
            errors.append(f"XML surah {s}: ayas={meta['suras'].get(s, {}).get('ayas')}")
        for a in range(1, AYAH_COUNTS[s - 1] + 1):
            if (s, a) not in tanzil:
                errors.append(f"Tanzil missing {s}:{a}")
            if (s, a) not in qcom:
                errors.append(f"Quran.com missing {s}:{a}")
    if len(tanzil) != 6236:
        errors.append(f"Tanzil has {len(tanzil)} verses, expected 6236")
    for k, exp in EXPECTED.items():
        if len(meta[k]) != exp:
            errors.append(f"metadata '{k}': {len(meta[k])} entries, expected {exp}")
    if errors:
        print("\nHARD CHECKS FAILED:\n  " + "\n  ".join(errors[:40]), file=sys.stderr)
        return 1

    # ---- Bismillah handling (Tanzil prepends it to ayah 1 of surahs 2..114 except 9)
    clean: dict[tuple[int, int], str] = {}
    stripped = 0
    for (s, a), t in sorted(tanzil.items()):
        if a == 1 and s not in (1, 9):
            t2, did = strip_bismillah(t)
            if not did:
                # source may already be bismillah-free; accept but make sure it's not odd
                if skeleton(t).startswith(BISMILLAH_SKEL):
                    errors.append(f"{s}:{a} bismillah present but not strippable")
            stripped += did
            t = t2
        elif skeleton(t).startswith(BISMILLAH_SKEL) and not (s == 1 and a == 1):
            errors.append(f"{s}:{a} unexpectedly starts with bismillah")
        clean[(s, a)] = t
    if errors:
        print("\nBISMILLAH CHECK FAILED:\n  " + "\n  ".join(errors[:40]), file=sys.stderr)
        return 1
    if clean[(1, 1)].split(" ")[0] == "" or len(clean[(1, 1)].split(" ")) != 4:
        print("1:1 should be exactly the 4-word bismillah", file=sys.stderr)
        return 1
    print(f"Bismillah stripped from {stripped} surahs "
          f"({'OK' if stripped in (0, 112) else 'UNEXPECTED, expected 112 or 0'})")

    # ---- Cross-check vs Source B (skeleton equality per ayah)
    diffs = []
    for key in sorted(clean):
        b = qcom[key]
        if key[1] == 1 and key[0] not in (1, 9):
            b, _ = strip_bismillah(b)          # in case B also carries it
        if skeleton(clean[key]) != skeleton(b):
            diffs.append((key, clean[key], b))
    report = out / "crosscheck_report.txt"
    with report.open("w", encoding="utf-8") as f:
        f.write(f"Ayahs compared: {len(clean)}   differing (consonantal skeleton): {len(diffs)}\n\n")
        for (s, a), x, y in diffs:
            f.write(f"{s}:{a}\n  Tanzil    : {x}\n  Quran.com : {y}\n\n")
    print(f"Cross-check: {len(clean) - len(diffs)}/{len(clean)} ayahs identical in skeleton; "
          f"{len(diffs)} differ -> {report}")
    if diffs and not args.lenient:
        print("Stopping: review the report (many may be harmless orthographic differences "
              "between the two editions), then re-run with --lenient to build anyway.",
              file=sys.stderr)
        return 2

    # ---- Build DB
    juz_of = marker_lookup(meta["juz"])
    quarter_of = marker_lookup(meta["quarter"])
    manzil_of = marker_lookup(meta["manzil"])
    ruku_of = marker_lookup(meta["ruku"])
    page_of = marker_lookup(meta["page"])
    sajda_at = {(s, a): (i, t) for i, s, a, t in meta["sajda"]}

    db_path = out / "quran.db"
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    try:
        con.executescript("""
        CREATE TABLE surahs (
            id INTEGER PRIMARY KEY, name_arabic TEXT NOT NULL, name_transliteration TEXT,
            name_english TEXT, revelation_type TEXT, revelation_order INTEGER,
            ayah_count INTEGER NOT NULL, word_count INTEGER NOT NULL,
            has_bismillah INTEGER NOT NULL);
        CREATE TABLE ayahs (
            id INTEGER PRIMARY KEY, surah_id INTEGER NOT NULL REFERENCES surahs(id),
            number_in_surah INTEGER NOT NULL, verse_key TEXT NOT NULL UNIQUE,
            text_uthmani TEXT NOT NULL, juz INTEGER, hizb INTEGER, hizb_quarter INTEGER,
            manzil INTEGER, ruku INTEGER, page INTEGER, sajda_type TEXT, sajda_index INTEGER,
            word_count INTEGER NOT NULL, sha256 TEXT NOT NULL,
            UNIQUE (surah_id, number_in_surah));
        CREATE TABLE words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ayah_id INTEGER NOT NULL REFERENCES ayahs(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, text_uthmani TEXT NOT NULL,
            UNIQUE (ayah_id, position));
        CREATE INDEX idx_ayahs_juz ON ayahs(juz);
        CREATE INDEX idx_ayahs_page ON ayahs(page);
        CREATE TABLE source_info (key TEXT PRIMARY KEY, value TEXT);
        """)
        gid = 0
        total_words = 0
        for s in range(1, 115):
            m = meta["suras"][s]
            swc = 0
            for a in range(1, AYAH_COUNTS[s - 1] + 1):
                gid += 1
                t = clean[(s, a)]
                words = t.split(" ")
                swc += len(words)
                q = quarter_of(s, a)
                sj = sajda_at.get((s, a))
                con.execute(
                    "INSERT INTO ayahs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (gid, s, a, f"{s}:{a}", t, juz_of(s, a), (q - 1) // 4 + 1, q,
                     manzil_of(s, a), ruku_of(s, a), page_of(s, a),
                     sj[1] if sj else None, sj[0] if sj else None,
                     len(words), hashlib.sha256(t.encode("utf-8")).hexdigest()),
                )
                con.executemany(
                    "INSERT INTO words (ayah_id, position, text_uthmani) VALUES (?,?,?)",
                    [(gid, i, w) for i, w in enumerate(words, 1)],
                )
            total_words += swc
            con.execute(
                "INSERT INTO surahs VALUES (?,?,?,?,?,?,?,?,?)",
                (s, m["name_arabic"], m["name_transliteration"], m["name_english"],
                 m["revelation_type"], m["revelation_order"], AYAH_COUNTS[s - 1], swc,
                 0 if s in (1, 9) else 1),
            )
        con.executemany("INSERT INTO source_info VALUES (?,?)", [
            ("text", "Tanzil Project - Uthmani (CC BY 3.0, https://tanzil.net)"),
            ("metadata", "Tanzil quran-data.xml"),
            ("cross_check", "Quran.com API v4 text_uthmani (consonantal skeleton)"),
            ("tanzil_text_sha256", hashlib.sha256(a_raw).hexdigest()),
            ("note", "Bismillah is stored only as 1:1; it is removed from ayah 1 of other surahs."),
        ])
        con.commit()

        # post-build verification straight from the DB
        chk = lambda q: con.execute(q).fetchone()[0]  # noqa: E731
        assert chk("SELECT COUNT(*) FROM surahs") == 114
        assert chk("SELECT COUNT(*) FROM ayahs") == 6236
        assert chk("SELECT COUNT(*) FROM words") == total_words
        assert chk("SELECT COUNT(DISTINCT juz) FROM ayahs") == 30
        assert chk("SELECT COUNT(DISTINCT hizb_quarter) FROM ayahs") == 240
        assert chk("SELECT COUNT(DISTINCT page) FROM ayahs") == 604
        assert chk("SELECT COUNT(*) FROM ayahs WHERE sajda_type IS NOT NULL") == 15
        assert chk("SELECT COUNT(*) FROM (SELECT DISTINCT surah_id, ruku FROM ayahs)") == 556
        assert chk("SELECT COUNT(*) FROM words w LEFT JOIN ayahs a ON a.id=w.ayah_id "
                   "WHERE a.id IS NULL") == 0
        print(f"\nDB OK: 114 surahs, 6236 ayahs, {total_words} words -> {db_path}")
    finally:
        con.close()

    (out / "quran.json").write_text(json.dumps({
        "source": "Tanzil Uthmani (CC BY 3.0) + quran-data.xml",
        "ayahs": [{"verse_key": f"{s}:{a}", "text": clean[(s, a)]} for (s, a) in sorted(clean)],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
