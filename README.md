# mangaP2ePub

スキャン済み PDF（紙の本を裁断してドキュメントスキャナで取り込んだ、1 ページ 1 画像の PDF）を、**固定レイアウト（漫画）ePub3** に変換する CLI ツールです。

出力は [EBPAJ 1.1.2](https://github.com/ebpaj/ebpaj-epub3-specs) のコミック仕様に準拠した pre-paginated EPUB3 で、ビューポートは 1103×1600 固定、右綴じ（`page-progression-direction="rtl"`）がデフォルトです。

---

## 特徴

- **PDF から埋め込み JPEG を無劣化で抽出**（1 ページ 1 画像前提）
- 全ページを **1103×1600 にフィット**（アスペクト比維持・白レターボックス）
- cover（表紙）は `page-spread-center`、本文は rtl で奇数=right / 偶数=left に自動割り付け
- TOC は最小 3 項目（**表紙 / 本編 / 奥付**）を `navigation-documents.xhtml` と `toc.ncx` の両方に出力
- ファイル名 `作品名_作者名.pdf` / `作品名-作者名.pdf` から **題名・作者を自動抽出**
- デフォルト出力名は `作品名_作者名.epub`（入力 PDF と同じディレクトリ）

---

## 必要環境

- Python 3.10 以上
- [pypdf](https://pypi.org/project/pypdf/)
- [Pillow](https://pypi.org/project/Pillow/)

```bash
pip install pypdf Pillow
```

---

## 使い方

### 最小呼び出し（ファイル名から自動取得）

```bash
python3 manga_p2epub.py 長い長いさんぽ_須藤真澄.pdf
```

上記だけで以下を行います：

- `長い長いさんぽ` を題名、`須藤真澄` を作者として抽出
- 同じディレクトリに `長い長いさんぽ_須藤真澄.epub` を生成

### オプションで上書き

```bash
python3 manga_p2epub.py scan.pdf \
    --title "作品名" \
    --author "著者名" \
    -o /path/to/output.epub \
    --force
```

### 全オプション

| オプション | 既定値 | 説明 |
|---|---|---|
| `pdf` (位置引数) | — | 入力 PDF（必須） |
| `-o`, `--output` | `<title>_<author>.epub` | 出力 EPUB パス |
| `--title` | ファイル名から抽出 | 題名を明示指定（ファイル名優先解決を上書き） |
| `--author` | ファイル名から抽出 | 作者を明示指定 |
| `--direction` | `rtl` | ページ送り方向（`rtl` または `ltr`） |
| `--quality` | `88` | JPEG 品質（1–95） |
| `--force` | オフ | 出力先が存在しても上書き |

---

## ファイル名からのメタデータ抽出ルール

入力 PDF の拡張子を除いたファイル名（stem）に対して、以下の順で区切り文字を探します：

1. アンダースコア `_`（優先）
2. ハイフン `-`（フォールバック）

最初に見つかった**1 回分**で分割し、前半を題名、後半を作者とします（`partition` 方式）。そのため題名や作者に区切り文字以外の `-` を含んでいても途中で壊れません。

| ファイル名 | 題名 | 作者 |
|---|---|---|
| `長い長いさんぽ_須藤真澄.pdf` | 長い長いさんぽ | 須藤真澄 |
| `作品名-作者名.pdf` | 作品名 | 作者名 |
| `A-B_C.pdf` | A-B | C |
| `no_separator.pdf` | no | separator |
| `タイトルのみ.pdf` | タイトルのみ | （なし → `unknown`） |

`--title` / `--author` を指定した場合は常にそちらが優先されます。

---

## 出力 EPUB の構造

```
mimetype                              (STORED、先頭固定)
META-INF/container.xml
item/standard.opf                     (EPUB3 OPF、EBPAJ 1.1.2 準拠)
item/navigation-documents.xhtml       (EPUB3 nav)
item/toc.ncx                          (EPUB2 互換 NCX)
item/style/fixed-layout-jp.css
item/xhtml/p-cover.xhtml              (表紙ページ SVG wrapper)
item/xhtml/p-001.xhtml
…
item/xhtml/p-NNN.xhtml
item/image/cover.jpg                  (1103×1600 に正規化)
item/image/i-001.jpg
…
item/image/i-NNN.jpg
```

各ページの XHTML は、SVG で画像 1 枚をビューポートに貼っただけの最小構造です：

```xml
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     version="1.1" width="100%" height="100%" viewBox="0 0 1103 1600">
  <image width="1103" height="1600" xlink:href="../image/i-001.jpg"/>
</svg>
```

### spine の配置ルール

- 1 ページ目（cover）: `rendition:page-spread-center`
- 2 ページ目以降（`rtl` 時）: 奇数番目 → `page-spread-right`、偶数番目 → `page-spread-left`
- `ltr` 指定時は左右が反転

### TOC の 3 項目

| ラベル | リンク先 |
|---|---|
| 表紙 | `xhtml/p-cover.xhtml` |
| 本編 | `xhtml/p-001.xhtml` |
| 奥付 | `xhtml/p-<最終ページ番号>.xhtml` |

`navigation-documents.xhtml` と `toc.ncx` の両方に同じエントリが入ります。2 ページ以下の PDF では項目数が自動的に減ります。

---

## 画像正規化の仕様

- すべて **1103×1600** にアスペクト比維持でフィット
- 余白は白（RGB は `(255,255,255)`、グレースケールは `255`）で埋める
- モードは `L`（グレースケール）か `RGB`。それ以外（CMYK, P など）は `RGB` に変換
- JPEG 品質 88（`--quality` で変更可）、`optimize=True`
- 既に 1103×1600 の画像もエンコーダ挙動を揃えるため再エンコードします

---

## 動作例

```
$ python3 manga_p2epub.py 長い長いさんぽ_須藤真澄.pdf
[info] title="長い長いさんぽ" (from filename), author="須藤真澄" (from filename)
[info] output -> /path/to/長い長いさんぽ_須藤真澄.epub
[info] extracting & normalizing pages from 長い長いさんぽ_須藤真澄.pdf
  page 1 ... 175.4 KB
  page 20 ... 483.5 KB
  …
  page 120 ... 236.0 KB
[info] writing /path/to/長い長いさんぽ_須藤真澄.epub
[done] /path/to/長い長いさんぽ_須藤真澄.epub (48.7 MB)
```

---

## スコープと制限（v0.1）

- **対象は漫画（固定レイアウト）専用**。小説・リフロー EPUB は対象外
- 1 ページに画像が**ちょうど 1 つ**埋め込まれている PDF を想定（多くのドキュメントスキャナ出力がこれに該当）
- 1 ページに画像が複数ある場合は**先頭の画像のみ**採用し警告を出す
- 画像を持たないベクター PDF は現状サポート外（将来 PyMuPDF でラスタライズ対応予定）
- OCR（透明テキスト層）は未実装
- 傾き補正・余白自動トリミングは未実装
- [epubcheck](https://github.com/w3c/epubcheck) による自動検証は未実装

## ロードマップ

- [ ] 元画像が viewport と同サイズなら再エンコードせず無劣化パススルー
- [ ] `--toc` オプションで任意の目次を読み込み
- [ ] `--cover-image` で表紙を差し替え
- [ ] `epubcheck` 連携（`--validate`）
- [ ] 傾き補正 / 白余白自動クロップ（オプション）
- [ ] GUI ラッパ（pywebview）

---

## ライセンス

[MIT License](LICENSE)
