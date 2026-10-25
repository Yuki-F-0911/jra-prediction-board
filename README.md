# jra-prediction-board

JRA当日の全レース予測を、最新の単勝オッズで約10分おきに作り直して GitHub Pages に出す。

- 各馬のモデル確率は当日中に変わらないため、手元の予測（horse-racing）から `predictions/YYYY-MM-DD.csv` に固定して置く。
- GitHub Actions（`board.yml`）が Yahoo!競馬の単勝オッズを取り、幾何ブレンド `objective^(1-w) * market^w` で合成し直す（特別・OP は w=0.5、それ以外 0.7）。標準ライブラリのみ。
- 最終レース発走の15分後まで動く。gh-pages は毎回1コミットで上書きする。

## 開催日の手順（PC）

```
# horse-racing で当日予測を実行したあと
python tools/export_predictions.py --race-date 2026-10-25
git add predictions && git commit -m "Add predictions 2026-10-25" && git push
```

push するとワークフローが起動する。土日は定期実行でも起動するので、前日までに push しておけば当日PCは不要。

予測は購入推奨ではない。
