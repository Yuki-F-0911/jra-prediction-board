"""horse-racing の当日予測CSVから、クラウド再計算用のモデル確率CSVを書き出す。

モデル確率（win_probability / top3_probability）は当日中に変わらないため、
ここで固定しておき、クラウド側では最新の単勝オッズとの合成だけを行う。

使い方（horse-racing の予測実行後に）:
    python tools/export_predictions.py --race-date 2026-10-25
"""

from __future__ import annotations

import argparse
import csv
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
]


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
            })
    race_count = len({row["race_id"] for row in rows})
    print(json.dumps({"output": str(output), "race_count": race_count, "horses": len(rows),
                      "missing_post_time": sorted(missing)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
