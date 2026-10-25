"""horse-racing の当日予測CSVから、クラウド再計算用のモデル確率CSVを書き出す。

モデル確率（win_probability / top3_probability）は当日中に変わらないため、
ここで固定しておき、クラウド側では最新の単勝オッズとの合成だけを行う。

使い方（horse-racing の予測実行後に）:
    python tools/export_predictions.py --race-date 2026-10-25
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from board.build import fetch_html  # noqa: E402

HORSE_RACING = Path(r"C:\Users\yuki0\projects\horse-racing")
COLUMNS = [
    "yahoo_race_id", "track_id", "race_number", "race_name", "post_time", "post_position", "gate",
    "horse_name", "jockey_name", "win_probability", "top3_probability", "market_weight",
    "objective_rank", "history_mapped", "form",
]
DETAIL_URL = "https://sports.yahoo.co.jp/keiba/race/denma/{race_id}?detail=1"


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _parse_run(cell: str) -> dict[str, str] | None:
    """出馬表「前走情報」の1走分。空セル（出走なし）は None。"""
    dates = re.findall(r'<p class="hr-denma__date">(.*?)</p>', cell, re.S)
    if len(dates) < 2:
        return None
    date_track = re.findall(r"<span>([^<]*)</span>", dates[0])
    course = _text(dates[1])
    course_match = re.match(r"(\S)\S*?(\d+)m\s*(\S*)", course)
    race = re.search(r'<p class="hr-denma__race">(.*?)</p>', cell, re.S)
    race_html = race.group(1) if race else ""
    finish = re.search(r'hr-denma__arrival[^"]*">([^<]*)<', race_html)
    name = re.search(r"<a [^>]*>([^<]*)</a>", race_html)
    grade = re.search(r'hr-label--[^"]*">([^<]*)<', race_html)
    field = re.search(r"(\d+)頭\s*(\d+)番\((\d+)人気\)", cell)
    jockey = re.search(r'hr-denma__jockey">.*?<a [^>]*>([^<]*)</a>', cell, re.S)
    margin = re.search(r'hr-denma__wHorse">.*?</a>\(([^)]*)\)', cell, re.S)
    last3f = re.search(r'hr-denma__data--time">[^<]*</span>\s*([\d.]+)', cell)
    passing = re.search(r'hr-denma__passing">([^<]*)<', cell)
    return {
        "date": date_track[0] if date_track else "",
        "track": date_track[1] if len(date_track) > 1 else "",
        "course": f"{course_match.group(1)}{course_match.group(2)}{course_match.group(3)}" if course_match else course,
        "finish": finish.group(1).strip() if finish else "",
        "race": (name.group(1).strip() if name else "") + (grade.group(1).strip() if grade else ""),
        "field": field.group(1) if field else "",
        "popularity": field.group(3) if field else "",
        "jockey": jockey.group(1).strip() if jockey else "",
        "margin": margin.group(1) if margin else "",
        "last3f": last3f.group(1) if last3f else "",
        "passing": passing.group(1) if passing else "",
    }


def fetch_form(race_id: str) -> dict[str, list[dict[str, str]]]:
    """馬番 -> 近5走（新しい順）。"""
    page = fetch_html(DETAIL_URL.format(race_id=race_id))
    numbers = re.findall(r'<p class="hr-denma__number">\s*(\d+)\s*</p>', page)
    body = page[page.find("js-scrollTbody"):]
    body = body[: body.find("</tbody>")]
    rows = re.findall(r"<tr>(.*?)</tr>", body, re.S)
    form: dict[str, list[dict[str, str]]] = {}
    for number, row in zip(numbers, rows):
        cells = re.findall(r'<td class="hr-tableScroll__data hr-tableScroll__data--race[^"]*"[^>]*>(.*?)</td>', row, re.S)
        form[number] = [run for run in map(_parse_run, cells) if run]
    return form


def history_mapping(md_path: Path) -> dict[str, str]:
    """当日予測Markdownの「履歴マッピング: x / y 頭」をレースIDごとに読む。"""
    mapping: dict[str, str] = {}
    if not md_path.exists():
        return mapping
    for section in re.split(r"(?m)^(?=### )", md_path.read_text(encoding="utf-8"))[1:]:
        race_id = re.search(r"denma/(\d+)", section)
        mapped = re.search(r"履歴マッピング: (\d+) / (\d+)", section)
        if race_id and mapped:
            mapping[race_id.group(1)] = f"{mapped.group(1)}/{mapped.group(2)}"
    return mapping


def fetch_post_times(meeting_ids: list[str]) -> dict[str, str]:
    post_times: dict[str, str] = {}
    for meeting_id in meeting_ids:
        list_html = fetch_html(f"https://sports.yahoo.co.jp/keiba/race/list/{meeting_id}")
        for number, post_time in re.findall(r"(\d{1,2})R\s*<p>\s*(\d{1,2}:\d{2})\s*</p>", list_html):
            post_times[f"{meeting_id}{int(number):02d}"] = post_time.zfill(5)
    return post_times


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--race-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--source-csv", help="省略時は horse-racing の scheduled 予測CSV")
    args = parser.parse_args()

    stamp = args.race_date.replace("-", "")
    source = Path(args.source_csv) if args.source_csv else (
        HORSE_RACING / "data/processed/objective_only/scheduled" / f"objective_predictions_{stamp}.csv"
    )
    with source.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    meeting_ids = sorted({row["race_id"].replace("web_", "")[:8] for row in rows})
    post_times = fetch_post_times(meeting_ids)
    race_ids = sorted({row["race_id"].replace("web_", "") for row in rows})
    forms = {race_id: fetch_form(race_id) for race_id in race_ids}
    mapped = history_mapping(HORSE_RACING / "docs/scheduled" / f"objective_predictions_{stamp}.md")

    output = Path(__file__).resolve().parents[1] / "predictions" / f"{args.race_date}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    missing: set[str] = set()
    with output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            race_id = row["race_id"].replace("web_", "")
            if race_id not in post_times:
                missing.add(race_id)
            writer.writerow({
                "yahoo_race_id": race_id,
                "track_id": row["track_id"],
                "race_number": row["race_number"],
                "race_name": row["race_name"],
                "post_time": post_times.get(race_id, ""),
                "post_position": row["post_position"],
                "gate": row["gate"],
                "horse_name": row["horse_name"],
                "jockey_name": row["jockey_name"],
                "win_probability": row["win_probability"],
                "top3_probability": row["top3_probability"],
                "market_weight": row["market_weight"],
                "objective_rank": row["objective_rank"],
                "history_mapped": mapped.get(race_id, ""),
                "form": json.dumps(forms[race_id].get(row["post_position"], []), ensure_ascii=False),
            })
    race_count = len({row["race_id"] for row in rows})
    with_form = sum(1 for race_id in race_ids for runs in forms[race_id].values() if runs)
    print(json.dumps({"output": str(output), "race_count": race_count, "horses": len(rows),
                      "horses_with_form": with_form, "missing_post_time": sorted(missing)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
