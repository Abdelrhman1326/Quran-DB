# Quran DB

Build a verified SQLite database of the Quran (Hafs from Asim, Uthmani script)
from the official Tanzil text, cross-checked against a second independent source.

Everything is reproducible: one script, pinned inputs, and checks that stop the
build if something is wrong.

## What you get

- `quran.db`: SQLite database (114 surahs, 6,236 ayahs, 77,433 words)
- `quran.json`: the clean ayah text, one entry per ayah

## Quick start

Requires Python 3.9+ (standard library only, no packages to install).

```bash
python3 build_quran_db.py --out quran-data
```

This downloads the sources, verifies them, and writes `quran-data/quran.db`.

### Build offline from the pinned files in `Data/`

```bash
python3 build_quran_db.py \
  --tanzil-file Data/tanzil-quran-uthmani.txt \
  --xml-file Data/quran-data.xml \
  --qcom-dir path/to/qcom_chapters \
  --out quran-data
```

`path/to/qcom_chapters` must contain `1.json` to `114.json`, the responses of
`https://api.quran.com/api/v4/quran/verses/uthmani?chapter_number=N`.

## How it works

| Role | Source |
|------|--------|
| Text (stored in the DB) | [Tanzil](https://tanzil.net) Uthmani text |
| Structure (juz, hizb, manzil, ruku, page, sajda) | Tanzil `quran-data.xml` |
| Cross-check only (never stored) | Quran.com API v4 `text_uthmani` |

### Checks (the build stops if any fail)

1. All 114 surahs have the standard number of ayahs; total is exactly 6,236.
2. Metadata totals: 30 juz, 240 hizb quarters, 7 manzils, 556 rukus, 604 pages, 15 sajdas.
3. The bismillah is stripped from ayah 1 of surahs 2-114 except 9 (exactly 112
   surahs). It is kept only as ayah 1:1.
4. Every ayah's letters (ignoring diacritics) must match the Quran.com text.
   Any difference is written to `crosscheck_report.txt` and the build stops;
   re-run with `--lenient` after reviewing the report to build anyway.
5. After building, the script re-verifies counts and foreign keys in the DB.

### What the checks do not cover

- The cross-check compares letters, not diacritics. A wrong vowel mark in the
  Tanzil text would not be detected.
- If both sources shared the same letter-level mistake, it would not be detected.
- For publication-grade use, spot-check a sample of pages against a printed
  Madinah mushaf.

## Numbering notes

- Follows Hafs / Kufan verse numbering (Madinah mushaf). The basmalah counts as
  ayah 1 of Al-Fatiha (`1:1`). In every other surah the basmalah is a heading,
  not an ayah, and is not stored in the ayah text.
- Other numbering systems (for example Madinan or Basran counting of
  Al-Fatiha) are not included.

## Database schema

```
surahs(id, name_arabic, name_transliteration, name_english,
       revelation_type, revelation_order, ayah_count, word_count, has_bismillah)

ayahs(id, surah_id, number_in_surah, verse_key, text_uthmani,
      juz, hizb, hizb_quarter, manzil, ruku, page,
      sajda_type, sajda_index, word_count, sha256)

words(id, ayah_id, position, text_uthmani)

source_info(key, value)
```

Example queries:

```sql
-- Ayat al-Kursi
SELECT text_uthmani FROM ayahs WHERE verse_key = '2:255';

-- All of Al-Fatiha
SELECT verse_key, text_uthmani FROM ayahs WHERE surah_id = 1;

-- Sajda verses
SELECT verse_key, sajda_type FROM ayahs WHERE sajda_type IS NOT NULL;
```

## Verify your build

The SHA-256 of the Tanzil file used is stored in the database:

```bash
sqlite3 quran.db "SELECT value FROM source_info WHERE key='tanzil_text_sha256';"
sha256sum Data/tanzil-quran-uthmani.txt
```

The two values should match. Each ayah also has its own `sha256` of its text.

## Display tip

Terminals do not shape Arabic correctly (letters look disconnected and
diacritics may be misplaced). View the data in a tool that renders Arabic, with a
Uthmani-capable font such as KFGQPC Uthmanic Hafs or Amiri Quran.

## Repo layout

```
.
├── build_quran_db.py        # the build script
├── Data/                    # pinned input files (Tanzil text and metadata)
└── README.md
```

## Credits and licence

- Quran text and metadata: [Tanzil Project](https://tanzil.net), licensed under
  [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). Attribution to
  tanzil.net is required. Tanzil asks that the text not be altered; this project
  removes only the repeated bismillah prefix from ayah 1 of surahs 2-114 (except 9)
  when building the database. See Tanzil's current terms at
  https://tanzil.net/docs/text_license
- Cross-check text: Quran.com API v4, used for verification only and not
  redistributed.
- Script: add your own licence here (for example MIT).
