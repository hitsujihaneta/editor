# MARCA — Marking Application for Rapid Contour Annotation
バウンディングボックス編集ツール
検出と追跡結果を手動で補正・可視化するツールです。

## 概要
２つのフェーズで役割が異なります．
- 検出フェーズ：検出結果を編集します．枠の編集（新規作成・削除・ID付与）を行い，未検出や誤検出の箇所を編集することで検出枠を精度100％にします．
- 追跡フェーズ：追跡結果を編集します．IDの編集（追跡IDの入れ替え等）を行い，誤追跡IDをなくすことで追跡IDを精度100％にします．

## 主な機能
- 追跡結果（BBox）のフレームごとの可視化
- バウンディングボックスの追加・編集・削除
- 追跡IDの付与・編集

## 使用技術
- Python / PyQt5
- .json,.txt,.csvのファイル形式に対応しています

## セットアップ

### 事前準備: Git
`git clone`とアプリ内の更新確認機能に必要です。未インストールの場合は先に入れてください。

- **Windows**: `winget install --id Git.Git -e --source winget`
  （`winget`が使えない場合は [git-scm.com](https://git-scm.com/download/win) からインストーラーを取得）
- **Mac**: 通常はプリインストール済みです。無い場合はターミナルで`git`と打つとインストールを促されます
  （もしくは `brew install git`）

インストール後は、ターミナル / Anaconda Promptを一度閉じて開き直してください（PATHの変更を反映させるため）。

### クローン方法
```bash
任意のフォルダに移動
git clone https://github.com/hitsujihaneta/MARCA.git
cd MARCA
pip install -r requirements.txt
```

### 起動スクリプト

コマンド使用
```
python main.py
```
起動スクリプトのクリック
- **Windows**: `start.bat` をダブルクリック
- **Mac**: `start.command` をダブルクリック

どちらも conda環境 `labelme` があれば自動で使い、無ければシステムのPython(`python3`)にフォールバックします。

## 更新の取得
アプリ内メニューの「🔄 更新 → 更新を確認...」から、リモートの最新コミットを取得して適用できます。
（ローカルに未コミットの変更がある場合は、安全のため更新は適用されません）

## イメージ
<img width="1440" height="900" alt="検出フェーズ" src="https://github.com/user-attachments/assets/46a56456-fb62-4df6-9919-17b4977b97bc" />
<img width="1440" height="900" alt="追跡フェーズ" src="https://github.com/user-attachments/assets/a8f2cf34-c4ad-4fec-9670-3e365d48417b" />
