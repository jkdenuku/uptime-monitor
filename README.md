# WatchTower — URL Monitor

UptimeRobotのようなURL監視ツール。FlaskとSQLiteで動作し、Renderに無料デプロイできます。

## 機能

- URLを追加して稼働状態を監視（UP/DOWN）
- 応答時間の記録
- 稼働率の表示（直近20回のチェック）
- 5分ごとの自動チェック（APScheduler）
- スパークラインによる履歴の可視化
- 手動チェックボタン

## ローカル起動

```bash
pip install -r requirements.txt
python app.py
```

ブラウザで http://localhost:5000 を開く

## GitHub → Render デプロイ手順

### 1. GitHubにアップロード

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/あなたのユーザー名/watchtower.git
git push -u origin main
```

### 2. Renderでデプロイ

1. https://render.com にアクセスしてサインアップ/ログイン
2. 「New +」→「Web Service」をクリック
3. GitHubリポジトリを接続して対象のリポジトリを選択
4. 以下の設定を確認：
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `bash start.sh`
5. 「Create Web Service」をクリック

### 3. （オプション）PostgreSQLを使う場合

1. Renderダッシュボードで「New +」→「PostgreSQL」を作成
2. Web ServiceのEnvironment Variablesに `DATABASE_URL` を追加
   （PostgreSQLのInternal Database URLをコピー）

## ファイル構成

```
watchtower/
├── app.py            # メインアプリ（Flask + APScheduler）
├── requirements.txt  # Pythonパッケージ
├── render.yaml       # Render設定
├── start.sh          # 起動スクリプト
└── templates/
    └── index.html    # フロントエンドUI
```

## 注意事項

- Renderの無料プランはスリープ機能があり、15分アクセスがないとスリープします
- スリープ中は監視チェックが行われません
- 本番利用には有料プランか、UptimeRobotなどで定期的にpingすることを推奨します
