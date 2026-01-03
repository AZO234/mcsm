[<img src="images/minecraft.svg" width="64" alt="Minecraft icon">](https://www.minecraft.net/) [<img src="images/modrinth.webp" width="64" alt="Modrinth icon">](https://modrinth.com/) [<img src="images/purpur.svg" width="64" alt="Modrinth icon">](https://purpurmc.org/) [<img src="images/papermc.svg" width="64" alt="Paper icon">](https://papermc.io/)

# mcsm（Minecraft Server & Plugin Manager）🧰

mcsm は、**Purpur / Paper** のサーバ本体と主要プラグインを導入・更新するための CLI ツールです。🚀

## 必要条件 🧪

- Python **3.11 以上**を推奨（`tomllib`）  
- Python **3.10** の場合は `tomli` が必要（`pip install tomli`）。

## クイックスタート ⚡

```bash
mkdir -p ~/servers/myserver
cd ~/servers/myserver
python3 mcsm.py install purpur 1.21.11
```

更新は以下です。

```bash
python3 mcsm.py update
```

## 対応 ✅

### サーバ
- Purpur 🟪
- Paper 📄

### プラグイン
- GeyserMC 🌉（GeyserMC API）
- Floodgate 🔐（GeyserMC API）
- ViaVersion 🧭（Modrinth API）

他のプラグインは `mcsm.toml` を編集して追加できます（例はコメントアウトで同梱しています）。✍️
Modrinth APIのインターフェイス名を登録してください。

## コマンド 🧾

- `init <platform>` ： 設定ファイルをテンプレート出力
- `list <platform> [mc_version]` : 一覧表示
- `install <platform> <mc_version>` : インストール
- `status` : 確認
- `update` : 更新
- `setup` : サーバをアプリとして登録
- `addsrv` / `rmsrv` : サーバをサービスとして登録＆削除

## 任意：mcsm をグローバルに配置する方法 🧩

`mcsm.py` を PATH の通った場所（例：`~/.local/bin`）に配置すると、どのサーバディレクトリからでも `mcsm` を呼び出せるようになります。

```bash
install -m 755 mcsm.py ~/.local/bin/mcsm
```

以降は、各サーバディレクトリで次のように実行できます。

```bash
cd ~/servers/myserver
mcsm install purpur 1.21.11
mcsm update
```

## ショートカットと自動起動 🖥️

- Windows：`setup` でスタートメニュー用 `.bat` を作成します。`addsrv` はスタートアップへコピーします。
- macOS：`setup` で `~/Applications` に `.command` を作成します。`addsrv` は **LaunchAgent（launchd）** で登録します。
- Linux：`setup` で起動用シェル + `.desktop` を作成します。`addsrv` は **systemd --user** で登録します。

## ライセンス 📜

MIT License

# 寄付のお願い！

[![Buy Me a Coffee](https://img.shields.io/badge/エメラルドなコーヒーを一杯おごって！-3C9A3C?style=for-the-badge&logo=minecraft)](https://coff.ee/azo234) ☕💚

[![Sponsor with Diamond](https://img.shields.io/badge/ダイヤモンドなスポンサーになって！-00ccff?style=for-the-badge&logo=minecraft)](https://github.com/sponsors/azo234) 💎✨
