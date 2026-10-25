"""開催日の確定結果・払戻を取り、予測を4方式で照合して results/ と成績ページ（results.html）を更新する。標準ライブラリのみ。

方式:
    朝版       その日最初に取れた単勝オッズで合成した予測
    直前版     確定前の最後のオッズで合成した予測（ページで最後に見られた順位）
    人気順     直前オッズの1〜3番人気
    モデル単独 客観モデルだけの順位
買い方は各レース100円: 単勝◎・複勝◎・ワイド◎-○・馬連◎-○・3連複◎○▲。取消馬を含む買い目は返還。
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path

from build import BASE_URL, blend, fetch_html, minutes, normalize

METHODS = ["朝版", "直前版", "人気順", "モデル単独"]
BETS = [("単勝", 1), ("複勝", 1), ("ワイド", 2), ("馬連", 2), ("3連複", 3)]
TEMPLATE = Path(__file__).with_name("results_template.html")


def _strip(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def parse_result_page(page: str) -> tuple[dict[int, dict], list[dict]]:
    """馬番 -> {finish, odds}、払戻の一覧。"""
    section = re.search(r'<section id="raceresult" class="hr-modCommon01 target_modules">(.*?)</section>', page, re.S)
    finishes: dict[int, dict] = {}
    for row in re.findall(r'<tr class="hr-table__row">(.*?)</tr>', section.group(1) if section else "", re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 9 or not _strip(cells[0]).isdigit():
            continue
        odds = re.search(r"\(([\d.]+)\)", _strip(cells[7]))
        finishes[int(_strip(cells[2]))] = {"finish": int(_strip(cells[0])), "odds": float(odds.group(1)) if odds else 0.0}
    payouts: list[dict] = []
    start, end = page.find("払戻金</h2>"), page.find("競走成績</h2>")
    label = None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page[start:end] if start != -1 and end != -1 else "", re.S):
        heads = re.findall(r"<th[^>]*>(.*?)</th>", row, re.S)
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if heads and _strip(heads[0]) not in {"", "馬番", "払戻金", "人気"}:
            label = _strip(heads[0])
        if label is None or len(cells) < 2:
            continue
        combo = _strip(cells[0]).replace(" ", "")
        value = _strip(cells[1]).replace(",", "").replace("円", "")
        if combo and value.isdigit():
            payouts.append({"type": label, "combo": combo, "pay": int(value)})
    return finishes, payouts


def _key(combo) -> str:
    return "-".join(str(n) for n in sorted(int(c) for c in combo))


def payout(payouts: list[dict], kind: str, combo: list[int]) -> int:
    target = _key(combo)
    return sum(p["pay"] for p in payouts if p["type"] == kind and _key(p["combo"].split("-")) == target)


def evaluate_race(horses: list[dict], odds_entry: dict, finishes: dict[int, dict], payouts: list[dict]) -> dict:
    final = {str(post): v["odds"] for post, v in finishes.items() if v["odds"] > 0}
    first = odds_entry.get("first") or odds_entry.get("odds") or final
    pre = odds_entry.get("pre") or final
    ranked: dict[str, list[dict]] = {}
    for method, odds in (("朝版", first), ("直前版", pre)):
        hs = [dict(h) for h in horses]
        blend(hs, odds)
        ranked[method] = hs
    by_odds = sorted((h for h in horses if float(pre.get(h["post_position"], 0)) > 0),
                     key=lambda h: (float(pre[h["post_position"]]), int(h["post_position"])))
    ranked["人気順"] = by_odds
    ranked["モデル単独"] = sorted(horses, key=lambda h: int(h["objective_rank"]))

    winner = next((post for post, v in finishes.items() if v["finish"] == 1), None)
    out = {"methods": {}, "result": sorted(finishes, key=lambda p: finishes[p]["finish"])[:3]}
    market = normalize([1 / float(pre[h["post_position"]]) if float(pre.get(h["post_position"], 0)) > 0 else 0 for h in horses], 1.0)
    model = normalize([float(h["win_probability"]) for h in horses], 1.0)
    win_prob = {
        "朝版": {int(h["post_position"]): h["p_win"] for h in ranked["朝版"]},
        "直前版": {int(h["post_position"]): h["p_win"] for h in ranked["直前版"]},
        "人気順": {int(h["post_position"]): p for h, p in zip(horses, market)},
        "モデル単独": {int(h["post_position"]): p for h, p in zip(horses, model)},
    }
    for method in METHODS:
        picks = [int(h["post_position"]) for h in ranked[method][:3]]
        bets = {}
        for kind, size in BETS:
            combo = picks[:size]
            bets[kind] = 100 if any(c not in finishes for c in combo) else payout(payouts, kind, combo)
        prob = win_prob[method].get(winner, 0.0) if winner else 0.0
        out["methods"][method] = {
            "picks": picks,
            "finish": [finishes.get(p, {}).get("finish") for p in picks],
            "bets": bets,
            "log_lik": math.log(max(prob, 1e-6)) if winner else None,
        }
    return out


def summarize(races: list[dict]) -> dict:
    summary = {}
    for method in METHODS:
        rows = [r["methods"][method] for r in races]
        lls = [r["log_lik"] for r in rows if r["log_lik"] is not None]
        summary[method] = {
            "races": len(rows),
            "win1": sum(r["finish"][0] == 1 for r in rows if r["finish"]),
            "top3": sum((r["finish"][0] or 99) <= 3 for r in rows if r["finish"]),
            "bets": {kind: {"hit": sum(r["bets"][kind] > 0 for r in rows), "ret": sum(r["bets"][kind] for r in rows),
                            "max": max((r["bets"][kind] for r in rows), default=0)} for kind, _ in BETS},
            "log_lik": sum(lls) / len(lls) if lls else None,
        }
    return summary


def pct(ret: float, n: int) -> str:
    return f"{ret / (n * 100):.0%}" if n else "—"


def render(results_dir: Path, site_dir: Path) -> None:
    esc = html.escape
    days = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(results_dir.glob("*.json"))]
    total = {m: {"races": 0, "win1": 0, "top3": 0, "ll": 0.0, "lln": 0,
                 "bets": {k: {"hit": 0, "ret": 0, "max": 0} for k, _ in BETS}} for m in METHODS}
    for day in days:
        for m in METHODS:
            s, t = day["summary"][m], total[m]
            t["races"] += s["races"]; t["win1"] += s["win1"]; t["top3"] += s["top3"]
            if s["log_lik"] is not None:
                t["ll"] += s["log_lik"] * s["races"]; t["lln"] += s["races"]
            for k, _ in BETS:
                for f in ("hit", "ret"):
                    t["bets"][k][f] += s["bets"][k][f]
                t["bets"][k]["max"] = max(t["bets"][k]["max"], s["bets"][k]["max"])

    def table(stats: dict, note_ex: bool) -> str:
        head = "".join(f"<th>{k}</th>" for k, _ in BETS)
        body = []
        for m in METHODS:
            s = stats[m]
            n = s["races"]
            cells = []
            for k, _ in BETS:
                b = s["bets"][k]
                ex = f'<small>除最大 {pct(b["ret"] - b["max"], n - 1)}</small>' if note_ex and n > 1 else ""
                cells.append(f'<td><b>{pct(b["ret"], n)}</b><small>{b["hit"]}/{n}的中</small>{ex}</td>')
            ll = s["ll"] / s["lln"] if "lln" in s and s["lln"] else s.get("log_lik")
            body.append(f'<tr><th scope="row">{m}</th><td>{s["win1"]}/{n}</td><td>{s["top3"]}/{n}</td>'
                        f'<td>{"" if ll is None else f"{ll:.3f}"}</td>{"".join(cells)}</tr>')
        return (f'<div class="tw"><table><thead><tr><th></th><th>◎1着</th><th>◎3着内</th><th title="勝ち馬に付けた確率の対数の平均。0に近いほど良い">対数尤度</th>'
                f'{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')

    sections = []
    for day in reversed(days):
        rows = []
        for r in day["races"]:
            res = " - ".join(str(p) for p in r["result"])
            picks = "".join(
                f'<td>{" - ".join(f"<span class={chr(34)}{"hit" if (f or 99) <= 3 else ""}{chr(34)}>{p}</span>" for p, f in zip(r["methods"][m]["picks"], r["methods"][m]["finish"]))}</td>'
                for m in ("直前版", "人気順"))
            rows.append(f'<tr><th scope="row">{esc(r["label"])}</th><td>{res}</td>{picks}</tr>')
        sections.append(
            f'<details class="day"{" open" if day is days[-1] else ""}><summary>{esc(day["race_date"])}（{day["race_count"]}レース）</summary>'
            f'{table(day["summary"], False)}'
            f'<div class="tw"><table class="races"><thead><tr><th>レース</th><th>1-2-3着</th><th>直前版 ◎○▲</th><th>人気順</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></details>')

    n_races = total[METHODS[0]]["races"]
    page = (TEMPLATE.read_text(encoding="utf-8")
            .replace("{{SPAN}}", f'{days[0]["race_date"]} 〜 {days[-1]["race_date"]}・{len(days)}日・{n_races}レース' if days else "まだありません")
            .replace("{{TOTAL}}", table(total, True) if days else "")
            .replace("{{DAYS}}", "\n".join(sections)))
    (site_dir / "results.html").write_text(page, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--race-date")
    parser.add_argument("--predictions")
    parser.add_argument("--odds-archive", help="build.py が出す archive/YYYY-MM-DD.json")
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--site-dir", required=True)
    parser.add_argument("--render-only", action="store_true", help="results/ から成績ページだけ作り直す")
    args = parser.parse_args()
    if args.render_only:
        render(Path(args.results_dir), Path(args.site_dir))
        return

    races: dict[str, list[dict]] = defaultdict(list)
    with open(args.predictions, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            races[row["yahoo_race_id"]].append(row)
    archive_path = Path(args.odds_archive)
    archive = json.loads(archive_path.read_text(encoding="utf-8")) if archive_path.exists() else {"races": {}}

    evaluated, missing = [], []
    for race_id, horses in sorted(races.items(), key=lambda item: (minutes(item[1][0]["post_time"]), item[0])):
        try:
            finishes, payouts = parse_result_page(fetch_html(f"{BASE_URL}/result/{race_id}"))
        except Exception as error:
            print(f"fetch_failed race={race_id} error={type(error).__name__}", flush=True)
            finishes, payouts = {}, []
        time.sleep(0.5)
        if not finishes or not payouts:
            missing.append(race_id)
            continue
        r = evaluate_race(horses, archive["races"].get(race_id, {}), finishes, payouts)
        r["race_id"] = race_id
        r["label"] = f'{horses[0]["track_id"]}{int(horses[0]["race_number"])}R {horses[0]["race_name"].replace("サラ系", "")}'
        evaluated.append(r)

    if missing:
        print(f"missing_results={','.join(missing)}", flush=True)
    if not evaluated:
        raise SystemExit("確定結果がまだありません")
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    day = {"race_date": args.race_date, "race_count": len(evaluated), "missing": missing,
           "summary": summarize(evaluated), "races": evaluated}
    (results_dir / f"{args.race_date}.json").write_text(json.dumps(day, ensure_ascii=False, indent=1), encoding="utf-8")
    render(results_dir, Path(args.site_dir))
    print(f"evaluated={len(evaluated)} missing={len(missing)}", flush=True)


if __name__ == "__main__":
    main()
