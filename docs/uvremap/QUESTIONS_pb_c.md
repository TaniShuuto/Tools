# PB-c 確認事項

- 要実機確認: Painter 12.1.5 で方向チャンネルの列挙名が
  `ChannelType.Normal` / `ChannelType.AnisotropyAngle` であること。
  テスト用スタブでは両方を確認したが、この環境には Painter API の実体が無い。
- 要実機確認: レイヤー数百のプロジェクトで、確認ダイアログ直前に一度だけ行う
  UV 投影フィル列挙の所要時間。失敗時もダイアログは表示する実装にした。
- 要実機確認: リベース Final 後に通常 Final が `_sync_result.json` を上書きしても、
  `rebase/results/<request_id>.json` の控えで Maya の完了判定が行われること。
- 要実機確認: `received` 状態に記録した待機理由の対処文が Maya の画面に表示され、
  理由が変わったときだけ表示と状態ファイルが更新されること。
