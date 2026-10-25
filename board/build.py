"""固定したモデル確率と最新の単勝オッズを合成し、予測ページ（index.html）を作る。標準ライブラリのみ。

合成は horse-racing の predict_objective_today_fast と同じ幾何ブレンド:
    hybrid ∝ objective^(1-w) * market^w   （w=market_weight: 特別・OP 0.5、それ以外 0.7）
市場勝率は正規化した 1/単勝オッズ。オッズ欠損馬は既知馬の最小逆数の半分で埋める。
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE_URL = "https://sports.yahoo.co.jp/keiba/race"
FINAL_STATUS = "確定オッズ"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TRACK_KEY = {"札幌": "sapporo", "函館": "hakodate", "福島": "fukushima", "新潟": "niigata", "東京": "tokyo",
             "中山": "nakayama", "中京": "chukyo", "京都": "kyoto", "阪神": "hanshin", "小倉": "kokura"}
MARKS = {1: "◎", 2: "○", 3: "▲", 4: "△", 5: "△"}
TEMPLATE = Path(__file__).with_name("template.html")


def fetch_html(url: str, attempts: int = 3) -> str:
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="ignore")
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(2 * attempt)
    raise AssertionError("unreachable")


def parse_win_odds(page_html: str) -> tuple[str, str, dict[str, float]]:
    status = re.search(r'hr-label--oddsStatus">\s*([^<]+?)\s*<', page_html)
    updated = re.search(r"(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})\s*更新", page_html)
    odds: dict[str, float] = {}
    table = re.search(r'<tbody[^>]*id="raceodds_table"[^>]*>(.*?)</tbody>', page_html, re.S)
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1) if table else "", re.S):
        numbers = re.findall(r'hr-tableValue__data--number">\s*(\d+)\s*</td>', row)
        cells = re.findall(r'hr-tableValue__data--odds">(.*?)</td>', row, re.S)
        if not numbers or not cells:
            continue
        for text in re.findall(r"<span[^>]*>\s*([^<]*?)\s*</span>", cells[0]):
            try:
                odds[numbers[-1]] = float(text)
                break
            except ValueError:
                continue
    return (status.group(1).strip() if status else "", updated.group(1) if updated else "", odds)


def normalize(values: list[float], target: float, clip: float | None = None) -> list[float]:
    """合計を target に揃える。clip があれば上限で頭打ちにし、余りを残りに配り直す。"""
    out = [0.0] * len(values)
    active = {i: max(v, 0.0) for i, v in enumerate(values)}
    remaining = target
    while active and remaining > 1e-9:
        total = sum(active.values())
        if total <= 0:
            share = remaining / len(active)
            for i in active:
                out[i] = min(share, clip) if clip else share
            break
        scale = remaining / total
        capped = [i for i, v in active.items() if clip is not None and v * scale >= clip]
        if not capped:
            for i, v in active.items():
                out[i] = v * scale
            break
        for i in capped:
            out[i] = clip
            active.pop(i)
            remaining -= clip
    return out


def blend(horses: list[dict], odds: dict[str, float]) -> None:
    n = len(horses)
    known = [1.0 / odds[h["post_position"]] for h in horses if odds.get(h["post_position"], 0) > 0]
    if known:
        fallback = min(known) * 0.5
        inverse = [1.0 / odds[h["post_position"]] if odds.get(h["post_position"], 0) > 0 else fallback for h in horses]
    else:
        inverse = [1.0] * n
    market_win = normalize(inverse, 1.0)
    market_top3 = normalize([p * 3 for p in market_win], min(3.0, n), clip=1.0)
    w = float(horses[0]["market_weight"])
    win_raw = [max(float(h["win_probability"]), 1e-9) ** (1 - w) * max(p, 1e-9) ** w for h, p in zip(horses, market_win)]
    top3_raw = [max(float(h["top3_probability"]), 1e-9) ** (1 - w) * max(p, 1e-9) ** w for h, p in zip(horses, market_top3)]
    for h, pw, p3 in zip(horses, normalize(win_raw, 1.0), normalize(top3_raw, min(3.0, n))):
        h["odds"] = odds.get(h["post_position"], 0.0)
        h["p_win"], h["p_top3"] = pw, p3
    horses.sort(key=lambda h: (-h["p_top3"], -h["p_win"], int(h["post_position"])))
    priced = sorted((h for h in horses if h["odds"] > 0), key=lambda h: h["odds"])
    popularity = {id(h): i for i, h in enumerate(priced, 1)}
    for rank, h in enumerate(horses, 1):
        h["rank"] = rank
        h["popularity"] = popularity.get(id(h))


def minutes(post_time: str) -> int:
    hour, minute = post_time.split(":")
    return int(hour) * 60 + int(minute)


def render_race(race_id: str, horses: list[dict], entry: dict) -> str:
    esc = html.escape
    first = horses[0]
    track, num, post_time = first["track_id"], int(first["race_number"]), first["post_time"]
    key = TRACK_KEY.get(track, "other")
    rows = []
    for h in horses:
        odds = f'{h["odds"]:.1f}' if h["odds"] > 0 else "—"
        pop = f'{h["popularity"]}人気' if h["popularity"] else "オッズなし"
        rows.append(
            f'<tr class="r{min(h["rank"], 6)}"><td class="mk">{MARKS.get(h["rank"], "")}</td>'
            f'<td class="no"><span class="waku w{esc(h["gate"])}">{esc(h["post_position"])}</span></td>'
            f'<td class="nm"><b>{esc(h["horse_name"])}</b><small>{esc(h["jockey_name"])}</small></td>'
            f'<td class="od"><b>{odds}</b><small>{pop}</small></td>'
            f'<td class="pw">{h["p_win"] * 100:.0f}<small>%</small></td>'
            f'<td class="p3"><span class="bar"><i style="width:{min(h["p_top3"], 1) * 100:.0f}%"></i></span>'
            f'{h["p_top3"] * 100:.0f}<small>%</small></td></tr>'
        )
    pick = " - ".join(h["post_position"] for h in horses[:3])
    status = entry.get("status") or "オッズ未取得"
    updated = entry.get("updated", "").split(" ")[-1]
    odds_note = f"{status} {updated}更新" if updated else status
    return (
        f'<details class="race" open data-track="{key}" data-min="{minutes(post_time)}" id="{key}{num}">'
        f'<summary><span class="tm">{post_time.lstrip("0")}</span><span class="trk t-{key}">{esc(track)}</span>'
        f'<span class="rn">{num}R</span><span class="rname">{esc(first["race_name"].replace("サラ系", ""))}</span>'
        f'<span class="st"></span><span class="pk">{pick}</span></summary>'
        f'<div class="tw"><table><thead><tr><th></th><th>馬番</th><th>馬名 / 騎手</th><th>単勝</th><th>勝率</th>'
        f'<th>3着内率</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        f'<p class="src">{len(horses)}頭 · {esc(odds_note)} · '
        f'<a href="{BASE_URL}/odds/tfw/{race_id}" target="_blank" rel="noopener">オッズ</a> · '
        f'<a href="{BASE_URL}/denma/{race_id}" target="_blank" rel="noopener">出馬表</a></p></details>'
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", required=True, help="tools/export_predictions.py の出力CSV")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--race-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--last-post", action="store_true", help="最終レースの発走時刻（分）を出力して終了")
    args = parser.parse_args()

    races: dict[str, list[dict]] = defaultdict(list)
    with open(args.predictions, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            races[row["yahoo_race_id"]].append(row)
    if args.last_post:
        print(max(minutes(horses[0]["post_time"]) for horses in races.values()))
        return

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / "odds_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    if cache.get("race_date") != args.race_date:
        cache = {"race_date": args.race_date, "races": {}}

    for race_id in races:
        entry = cache["races"].get(race_id, {})
        if entry.get("status") == FINAL_STATUS:
            continue
        try:
            status, updated, odds = parse_win_odds(fetch_html(f"{BASE_URL}/odds/tfw/{race_id}"))
        except Exception as error:
            print(f"fetch_failed race={race_id} error={type(error).__name__}", flush=True)
            continue
        if odds:
            cache["races"][race_id] = {"status": status, "updated": updated, "odds": odds}
        time.sleep(0.5)

    cards = []
    for race_id, horses in sorted(races.items(), key=lambda item: (minutes(item[1][0]["post_time"]), item[0])):
        entry = cache["races"].get(race_id, {})
        blend(horses, entry.get("odds", {}))
        cards.append(render_race(race_id, horses, entry))

    year, month, day = (int(part) for part in args.race_date.split("-"))
    tracks = "・".join(dict.fromkeys(horses[0]["track_id"] for horses in races.values()))
    page = (TEMPLATE.read_text(encoding="utf-8")
            .replace("{{CARDS}}", "\n".join(cards))
            .replace("{{TITLE}}", f"{month}/{day} 全レース予測")
            .replace("{{HEADING}}", f"{year}年{month}月{day}日 全{len(races)}レース予測")
            .replace("{{TRACKS}}", tracks)
            .replace("{{BUILT_AT}}", datetime.now().strftime("%H:%M"))
            .replace("{{DATE_JSON}}", json.dumps([year, month - 1, day]))
            .replace("{{TRACK_BUTTONS}}", "".join(
                f'<button type="button" data-f="{TRACK_KEY.get(t, "other")}" aria-pressed="false">{t}</button>'
                for t in dict.fromkeys(horses[0]["track_id"] for horses in races.values()))))
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"built races={len(races)} with_odds={sum(1 for r in races if cache['races'].get(r))}", flush=True)


if __name__ == "__main__":
    main()
