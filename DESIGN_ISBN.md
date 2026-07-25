# DESIGN — ISBN 正規化・自動検出とOPF書誌タグ網羅

対象: `mangaP2ePub/manga_p2epub.py`（主）／ `jisui2epub/jisui2epub.py`（横展開）
起票: 2026-07-25　ステータス: フェーズ1=仕様確定・未実装 / フェーズ2=検討メモ

---

## 1. 背景と課題

### 1.1 現行 `--isbn` のバグ
両ツールとも `re.sub(r"[-\s]", "", isbn)` のみで、`dc:source urn:isbn:…` を生成している。

- mangaP2ePub: `build_bib_meta()` — manga_p2epub.py:269
- jisui2epub: `_build_opf()` 相当 — jisui2epub.py:4488

このため `--isbn "ISBN978-4-06-377048-3"` は `urn:isbn:ISBN9784063770483` となり、
`ISBN` 接頭辞・全角ハイフン（`－ ‐ ‑`）・全角空白・コロンが残る。
yomikake の国立国会図書館サーチは **数字13桁のみ** を期待するため、リンクが該当ページに到達しない。

### 1.2 自動化されていない
ISBN は通常 **裏表紙のバーコード** または **奥付ページ** に印刷される。自炊PDFでは末尾数ページ
（広告があるとその分だけ奥付は前へずれる）に位置し、多くは「ISBN」キーワードを伴う。
ScanSnap 等の OCR テキスト層が残っているので、`--isbn` 未指定時に自動取得できる余地が大きい。

### 1.3 ISBN の年代（未検出本の解釈）
ISBN 制定は 1970 年、日本での運用開始は **1981 年**。それ以前の刊行物や一部の小部数出版物には
ISBN バーコードが存在しない。したがって「検出できない本」は概ね **本自体に ISBN が無い** ケースであり、
自動検出は best-effort で十分（未検出時は `--isbn` 手動指定で補完）。

---

## 2. 実測調査（mangaP2ePub/temp_sample、pypdf `extract_text` で末尾8ページ走査）

| 本 | 結果 | 検出値（正規化後 ISBN-13） | 備考 |
|---|---|---|---|
| 寄生獣１ | ✅ | 9784063770483 | 「ISBN」有・EAN有 |
| ひだまりスケッチ１ | ✅ | 9784832275492 | **裸EAN-13のみ**（ISBN文字なし） |
| だもんで豊橋が好き７ | ✅ | 9784801984387 | 「ISBN」有 |
| アクアリウム | ✅ | 9784881991107 | 奥付ISBN-10(4-88199-110-8)＋裏バーコードEAN一致 |
| 志乃ちゃん | ✅ | 9784778321802 | 全角ハイフンあり |
| 電気ブラン | ✅ | 9784812450277 | ISBN-10(4-8124-5027-6)→13桁化 |
| 絶対安全剃刀 | ✅ | 9784592760160 | ISBN-10(4-592-76016-6)→13桁化 |
| 白銀の墟玄の月1 | ✅ | 9784101240626 | 「ISBN」有 |
| チュー坊がふたり | ✅ | 9784062607940 | **裸EAN-13のみ** |
| 陽だまりの風景（阿保美代） | — | （なし） | 全ページ走査でもISBN/EAN皆無 |
| ほんのすこしの水（岡田史子） | — | （なし） | 同上 |
| ガラス玉（岡田史子） | — | （なし） | 同上 |

**検出率 9/12。** 未検出3冊はテキスト層に ISBN/EAN が一切存在しない（§1.3）。

### 2.1 誤検出リスク（重要）
コミック裏表紙は上下2段のバーコードを持つ。**下段は価格コード `192…`**（例: 寄生獣 `1929979004637`）。
これは **EAN-13 のチェックディジットを通過してしまう**ため、チェックサム単独では弾けない。
→ 正規表現を **`97[89]` 始まりの13桁** にアンカーすることで確実に除外する（`192…` は不一致）。

---

## 3. フェーズ1 仕様（確定）

ユーザー承認済みの方針:
1. 自動検出は **裸の EAN-13 も採用**（「ISBN」文字が無くても `97[89]`＋13桁＋チェックサムなら可）
2. 出力は **ISBN-13 に統一**（ISBN-10 は `978` 付与で13桁化）
3. 埋込先は **`dc:source urn:isbn` のみ**維持（`dc:identifier` は `urn:uuid` のまま）

### 3.1 `normalize_isbn(raw: str) -> str | None`（新設・共通ロジック）
`--isbn` 明示指定と自動検出の**両方**が通す。

```
1. NFKC 正規化（全角ハイフン ‐‑－・全角空白　・全角数字・全角ＩＳＢＮ を半角へ畳む）
2. 行頭の "ISBN[:：]?" 接頭辞を除去（大小文字無視）
3. 数字と X 以外を全除去
4. 13桁 かつ 97[89] 始まり かつ mod-10 チェック通過 → その13桁を返す
5. 10桁 かつ mod-11 チェック通過 → "978"+先頭9桁 に再計算した検査数字を付け13桁化して返す
6. それ以外 → None
```

- チェックサム: ISBN-13 = Σ(奇数位×1＋偶数位×3) mod 10 == 0 / ISBN-10 = Σ(位×(10-i)) mod 11 == 0（X=10）。
- `urn:isbn:9784…` を渡しても（`isbn` が先頭でなくても）数字抽出で正しく13桁化される（冪等）。

### 3.2 `detect_isbn_from_pdf(pdf_path, max_pages=10) -> str | None`（新設）
- 実行条件: `build_epub(detect_isbn=True)`（＝`--isbn` 未指定 かつ `--no-isbn-detect` 未指定）。
- 走査範囲: **末尾10ページ**をページ単位で NFKC 正規化。**最終ページから先頭方向**へ走査（奥付は末尾寄りのため）。
- **2パス構成**（実装で洗練。当初案の「キーワード優先」より安全）:
  - **パス1: 裸の `97[89]` EAN-13 を最優先**。数字境界の後読み/先読み（`(?<!\d)…(?!\d)`）で
    価格コード `192…` の一部への誤マッチを排除。EAN-13 はキーワード有無に依らず確実に13桁を切り出せ、
    「13桁統一」方針とも一致するため全ページで先に探す。
  - **パス2: ISBN-10（旧奥付）フォールバック**。「ISBN」キーワード直後 25 文字の窓に限定して
    ちょうど10桁トークンを探す（隣接する C コード・価格・ノンブルへの過走を防止）。
    電気ブラン/絶対安全剃刀/アクアリウム のような ISBN-13 バーコード非掲載の旧本を拾う。
- 全候補を `normalize_isbn()` で検証し、最初に通ったものを採用。
- ログ: 成功 `[isbn] auto-detected 9784… (page N, EAN-13|ISBN-10 colophon)` / 失敗 `[isbn] not found in last N page(s)`。

実際の正規表現（`_ISBN_SEP = r'[-‐‑\s]?'`、対象テキストは NFKC 済み）:
```
_EAN13_RE  = (?<!\d)97[89](?:_ISBN_SEP \d){10}(?!\d)
_ISBN10_RE = (?:\d _ISBN_SEP){9}[\dX]        # 「ISBN」直後の窓内のみに適用
```

### 3.3 埋込（変更なし方針の明文化）
`dc:source>urn:isbn:{13桁}` のみ。`unique-identifier` は従来どおり `urn:uuid`。
→ yomikake の NDL サーチ連携と完全互換。

### 3.4 実装済みの変更点（mangaP2ePub、2026-07-25 完了）
- `unicodedata` を import。ISBN ヘルパ群（`_isbn13_check_ok`/`_isbn10_check_ok`/`_isbn10_to_13`/
  `normalize_isbn`/`detect_isbn_from_pdf`）を `build_bib_meta` 直前に新設。
- `build_bib_meta()` の旧 `re.sub(r"[-\s]",…)` を `normalize_isbn()` に置換（現行バグ解消）。
- `build_epub()` に `detect_isbn: bool = True` 引数を追加。関数冒頭で ISBN を解決:
  明示 `--isbn` は `normalize_isbn` を通し、**不正なら `[isbn] ignoring invalid …` と警告して自動検出にフォールバック**。
- `main()` の argparse に `--no-isbn-detect` を追加、`--isbn` の help を更新、`build_epub` へ `detect_isbn` を伝搬。

---

## 4. スコープ方針 — mangaP2ePub 単体で個別実装（jisui2epub とは統合しない）

**決定（2026-07-25）**: 今回の改修は **mangaP2ePub 単体で完結**させる。jisui2epub との
プログラム統合・共通モジュール化は**見送る**。

理由:
- 2ツールの核（画像・色・クロップ・固定レイアウト vs 本文再構築・ルビ・リフロー）と依存
  （PIL+pypdf+pdfminer vs PyMuPDF）は別物で、共通なのは書誌タグの末尾のみ。
- 利用者から見た一本化は既に `jisui_gui.py`（novel/horizontal/manga セレクタで dispatch）で達成済み。
- jisui2epub は今後「章構成・目次生成」など重い本文処理に注力する段階で、mangaP2ePub は軽微修正のみの想定。
  それぞれの課題に**別々に取り組む**方が保守が明快。
- したがって `normalize_isbn()` 等は共有せず、jisui2epub 側は必要時に**独立して個別実装**する
  （同一課題 :4488 は将来別タスクで対応）。

本設計書は以降 **mangaP2ePub の実装仕様**として扱う。

---

## 5. フェーズ2 準備 — OPF 書誌タグの網羅（参考epub 11冊の実証分析）

### 5.1 参考コーパス（計11冊、2026-07-25 時点）
- **第1群（yomikake/temp_sample・EPUB3）**: 蘇我氏 / 仏教入門 / ねらわれた学園（新装版）/ 平安朝の事件簿
- **第2群（mangaP2ePub/temp_sample・今回追加）**:
  - EPUB3: `ITエンジニアのためのMarkdown実践入門`(技術評論社) / `oreilly 778`(退屈なことはPythonに) / `oreilly 753`(Pythonチュートリアル)
  - EPUB2: `オープンソースライセンス`(達人出版会) / `oreilly 552`(EPUB3とは何か) / `ナルニア`(岩波少年文庫) / `RDG4`(角川文庫)

> EPUB2 は `opf:role` / `opf:file-as` / `opf:scheme` / `opf:event` を dc 要素の**属性**で表す。
> EPUB3 は `<meta refines property="…">` で表す。当ツールは version="3.0" なので **EPUB3(refines) 方式**を採る。

### 5.2 識別子戦略は「本の素性」で割れる（重要）
| epub | 識別子 (`unique-identifier`) | ISBN の扱い |
|---|---|---|
| 蘇我氏/仏教/ねらわれた/平安/Markdown | `urn:uuid` | 出さない |
| ナルニア / RDG4 | `urn:uuid` ＋ 第2識別子 `opf:scheme="ASIN"` | ASIN のみ（Kindle 由来） |
| oreilly 778 / 753 | **裸の ISBN-13**（`9784873117782`） | 識別子そのものが ISBN |
| oreilly 552 | `opf:scheme="ISBN"` の 13桁 | 同上 |
| オープンソースライセンス | 販売 URL | 出さない |

**解釈**: ISBN を識別子に使うのは**その epub 自体が正規の ISBN 商品**である場合（O'Reilly 等）。
当ツールの出力は**紙の本を個人が自炊した複製**なので、
- `dc:identifier`(=この電子ファイルの id) は `urn:uuid`
- `dc:source`(=**底本**＝派生元の紙の本) は `urn:isbn`

とするのが意味論的に最も正しい。→ **フェーズ1の「ISBN は dc:source のみ・識別子は uuid」判断はフェーズ2でも維持**する
（ISBN を第2 `dc:identifier` に併記すると「このファイル＝紙の本」と誤って主張することになる）。

### 5.3 タグ網羅マトリクス（当ツール現状 vs 参考の採用状況）
| タグ / 役割 | 参考での出現 | 当ツール現状 | フェーズ2 方針 | 取得元 |
|---|---|---|---|---|
| `dc:title` | 全冊 | ✅ | 維持 | — |
| `dc:identifier` urn:uuid | 大半 | ✅ | 維持 | 生成 |
| `dc:language` | 全冊 | ✅ | 維持 | 固定 ja |
| `dcterms:modified` | EPUB3 全冊 | ✅ | 維持 | 生成 |
| `dc:creator`＋`role`＋`display-seq` | 多数 | ✅(aut/art) | 維持 | 引数/ファイル名 |
| `dc:publisher` | 多数 | ✅任意 | 維持 | `--publisher` |
| `dc:date` | 多数 | ✅任意 | 維持＋**奥付自動抽出を検討** | `--date`/奥付 |
| `dc:source` urn:isbn | （独自運用） | ✅(今回強化) | 維持 | `--isbn`/自動 |
| **`file-as`**（title/creator/publisher の読み） | 蘇我/仏教/ねらわれた/平安/Markdown/753 | ❌ | **追加**（refines） | 新規 CLI 引数 |
| **`dc:rights`**（著作権表示） | oreilly 778/753/552, オープンソース | ❌ | 追加（任意） | `--rights` or 自動 |
| **`dc:description`**（内容紹介） | オープンソース | ❌ | 追加（任意） | `--description` |
| `dc:contributor`＋role（edt/trl/bkp/prt/pbl/ill） | oreilly 群, オープンソース | ❌ | 低優先（翻訳漫画向けに `trl`/`ill` 検討） | 新規 CLI |
| アクセシビリティ（`schema:accessMode` 等） | 無 | ❌ | 追加検討（下記 5.5） | 生成 |
| `ibooks:*` / `generator` / `yznet:*` | 一部 | — | **採用しない**（各社独自/変換器痕跡） | — |

### 5.4 実装上の要点（file-as を入れる場合）
現行 OPF テンプレは `dc:title` に `id` が無く、`dc:publisher` も plain 出力。file-as を `refines` で
付けるには **被参照要素へ id を付与**する必要がある。
- `dc:title` → `id="title"`、`dc:publisher` → `id="publisher"` を付与。
- creator は既に `id="creatorNN"` 済み → そのまま `<meta refines="#creatorNN" property="file-as">…` を追加可能。
- 追加 CLI: `--title-kana` / `--author-kana` / `--artist-kana` / `--publisher-kana`（全て任意・省略時は出力しない）。
- カナは PDF から自動取得不可（読み推定は誤りやすい）ため**手動指定のみ**とする。

### 5.5 EPUB3.2 / アクセシビリティ準拠の指針
- 参照仕様: EPUB 3.2 Packages（`sec-opf-dc-identifier`）＋ EPUB Accessibility 1.1 / imagedrive 版仕様（ユーザー提示）。
- 必須3要素（identifier / title / language）＋ `dcterms:modified` は充足済み → **基礎的な妥当性は既にクリア**。
- 固定レイアウト画像漫画は本来 `accessMode=visual` だが、**当ツールは透明 OCR テキスト層を埋め込む**ため
  `textual` も主張し得る。フェーズ2で以下を任意付与する案:
  ```
  <meta property="schema:accessMode">visual</meta>
  <meta property="schema:accessMode">textual</meta>          ← OCR層あり時
  <meta property="schema:accessModeSufficient">visual</meta>
  <meta property="schema:accessibilityFeature">none</meta>   ← or "unknown"
  <meta property="schema:accessibilityHazard">unknown</meta>
  <meta property="schema:accessibilitySummary">…</meta>
  ```
  （OCR 層の有無＝`--no-text` の状態で accessMode を出し分けられる点が当ツールの強み。）

### 5.6 あらすじ／発行年月の自動取得 実証（temp_sample 12冊、2026-07-25）

**あらすじ（dc:description）→ 自動取得は不可。`--description` 手動のみ。**
- 全ページ走査で「あらすじ/内容紹介/ストーリー」等のヒットは**全て誤検出**
  （ひだまり=ギャグ内「ストーリー作成カード」・初出一覧、白銀=巻末の他書広告）。
- あらすじは**裏表紙・帯・折返し**に印刷され、自炊は内側ページのみスキャンのため含まれない
  （末尾のISBN/奥付は拾えるが裏表紙は無い、という §2 と同じ構造）。構造的マーカーも無く分離不能。

**発行年月（dc:date）→ 自動取得は実用的。奥付から検出、失敗時は付与しない。**
- 発行日は**12/12 全冊の奥付に存在**。試作パーサ（末尾ページの `発行` 近傍・西暦/和暦/漢数字対応・
  和暦→西暦変換・`初版/第1刷`優先・OCR化け時は年月へ縮退）で **10/12 EXACT・WRONG 0・MISS 2**。
- 対応形式の実例: 西暦「2005年11月11日」/ 和暦「昭和57年1月19日」「令和元年**十月十二日**（漢数字）」。
- MISS 2件は OCR 化け（アクアリウム「1994年**e**月」＝月が非数字、ほんのすこし「**昭刷153**年」＝元号破損）で
  **年月が壊れた場合のみ**。**誤った日付は出さない（WRONG=0）**＝ユーザー可視フィールドとして安全な失敗モード。
- 「初版 vs 第N刷」問題（ひだまり=第26刷2012 と 初版2005 が並ぶ）は `初版/第1刷` 近傍優先＋最小日付で
  初版2005を正取得。絶対安全剃刀（初版1982／第46刷2025）・陽だまり（第1刷／第4刷）も初版側を取得。

**スキャン日／ePub化日は dc:date に使わない（重要）**:
- `dc:date` の意味論は**原刊行日**。スキャン日/変換日を入れると書誌表示で誤情報になる。
- サンプルPDFの `/CreationDate` は全て `None`（pypdf/PyPDF2 で再処理済みでスキャン日は喪失）＝そもそも取得不可。
- **ePub化日時は既に `dcterms:modified`（=生成時刻）に記録済み**。役割が分離されているので二重化は不要。
- 結論: `dc:date` = 奥付発行日（検出時）/ `--date`（手動優先）/ どちらも無ければ**出力しない**。

### 5.7 NDL（国立国会図書館サーチ）書誌照会 実証（2026-07-25）
ISBN を鍵に **NDL OpenSearch API** で権威的書誌を取得できる。OCR/手動では困難だった**読み仮名・役割**まで揃う。

- エンドポイント: `https://ndlsearch.ndl.go.jp/api/opensearch?isbn=<13桁>`（RSS/XML、認証不要）。
- 取得できる主フィールド（実測）:
  | XML 要素 | 内容 | 用途 |
  |---|---|---|
  | `dc:title` / `dcndl:titleTranscription` | 書名 / 書名の読み | title の file-as |
  | `dc:creator`（正規形 "姓, 名"） | 著者正規形（生年付き例 "田淵, 由美子, 1954-"） | 参考 |
  | `dcndl:creatorTranscription` | 著者の読み "アオキ, ウメ" | creator の file-as |
  | **`<author>` / description の「責任表示」** | **自然形＋役割**（"蒼樹うめ 著" / "Al Sweigart 著,相川愛三 訳"） | dc:creator 本文・role 判定・複数著者分離 |
  | `dc:publisher` | 出版社 | dc:publisher 補完 |
  | `dcterms:issued`（YYYY.M）/ `dc:date`（YYYY） | 発行年月 | dc:date |
  | `dcndl:seriesTitle` / `dcndl:volume` | シリーズ名 / 巻 | 任意 |
  | `dc:subject`（NDC9/NDLC）/ `dcndl:genre` | 分類 / ジャンル | 任意 |
- **役割語→marc:relators**: 著→`aut` / 訳→`trl` / 編→`edt` / 画・作画→`art` / 原作→`aut`（原作）。責任表示のカンマ区切りで複数著者・役割を分離可能。
- **カバレッジ（検出ISBN 9件中）**: ヒット **7/9**（ひだまり・寄生獣・だもんで・志乃・電気ブラン・白銀・チュー坊）。
  未ヒット **2/9**（絶対安全剃刀=白泉社文庫版・アクアリウム=新声社）。ISBN-10形・タイトル検索でも 0 件＝**NDL索引の穴**（API問題ではない）。
  ISBN 自体が無い 3 冊と併せ、**全12冊中 7冊が NDL 補完可**。
- **あらすじ（dc:description）は NDL も提供しない**（description は年のみ）→ §5.6 のとおり `--description` 手動のまま。
- **注意**: ①ネットワーク必須 ②NDL字形が実本/ファイル名と異体字で相違し得る（田淵 vs 田渕）③書名は巻数を `dcndl:volume` に分離。

### 5.8 しおりキーへの影響（yomikake 実装確認）
yomikake のしおりキーは `makeBookKey(title, creator) = 'epub_pos_' + title + '__' + creator`
（yomikake.html:7555。**title＋creator 由来、`urn:uuid` 非依存**）。
→ **dc:creator が変わるとブックマークが割れる**。title はファイル名固定なので安定。
creator を**既定＝ファイル名/CLI** にすれば creator が再現的でキーが安定するため、この既定が
しおり安定性の観点でも正しい。`--creator-source ndl` 採用時はキーが NDL 形に依存する旨を注記する。

### 5.9 フェーズ2 確定仕様（ユーザー決定・実装時はこの通り）
| 項目 | 決定 |
|---|---|
| **NDL 照会** | 既定 **ON**。ISBN 取得時に OpenSearch 照会。オフライン/未ヒット/エラー/タイムアウトは**警告のみで続行**。`--no-ndl` で無効化。短いタイムアウト＋1回のみ。 |
| **dc:creator 本文の源** | CLI スイッチ **`--creator-source {filename,ndl}`、既定 `filename`**。`filename`=ファイル名/`--author`/`--artist`。`ndl`=NDLヒット時は責任表示の自然形を採用。**`--author`/`--artist` 明示は常に最優先**。ファイル名とNDL形が食い違えば警告ログ。 |
| **file-as（読み）** | NDL の titleTranscription/creatorTranscription を refines で付与。CLI `--title-kana`/`--author-kana`/`--artist-kana`/`--publisher-kana` は上書き・オフライン用。 |
| **役割 / 追加著者** | 責任表示から role 判定（著/訳/編/画）。翻訳・編纂・原作作画分離は contributor/creator を追加（creator-source=filename でも訳者等の**追加役割者のみ**NDL補完は任意可）。 |
| **dc:date** | 優先順: `--date`（手動）> NDL `dcterms:issued` > **OCR 奥付検出（§5.6、10/12・WRONG 0）** > 無し。**スキャン/変換日は使わない**（変換日は `dcterms:modified`）。`--no-date-detect` を用意。 |
| **dc:publisher** | `--publisher` 未指定時に NDL 補完。 |
| **series/volume/NDC** | 任意付与（`dcndl:seriesTitle`+`volume`、`dc:subject` NDC）。名前空間 prefix 追加要。 |
| **dc:description（あらすじ）** | `--description` **手動のみ**（NDL/OCR とも不可）。 |
| **アクセシビリティ** | `schema:accessMode`=visual（**OCR層あり時 textual 追加**）/`accessModeSufficient`/`accessibilityFeature`/`Hazard`/`Summary`。`--no-text` 時は visual のみ。 |
| **dc:rights** | **自動生成なし**（自炊）。`--rights` 任意・低優先。 |
| **識別子** | `urn:uuid` 維持、ISBN は `dc:source`（フェーズ1踏襲、§5.2 の意味論）。 |

**フィールド優先順位（原則）**: CLI 明示 > NDL > ファイル名/OCR。ただし **creator/title の本文は既定でファイル名 > NDL**（creator-source で切替）。

実装単位の候補（着手時に順序確定）:
1. `fetch_ndl_by_isbn(isbn)` — OpenSearch 照会・パース（責任表示/読み/発行/版元/シリーズ）。`--no-ndl`。
2. file-as（title/publisher へ id 付与＋refines）＋読みの NDL/CLI 統合。
3. `--creator-source` 切替と食い違い警告、役割・追加著者。
4. dc:date（NDL→OCR奥付→手動、§5.6 の試作を `detect_pubdate_from_pdf()` 化、`--no-date-detect`）。
5. アクセシビリティ metadata（OCR層連動）。
6. dc:description / dc:rights / series・NDC（任意群）。

### 5.10 フェーズ2 実装内容（2026-07-25 完了）
manga_p2epub.py への追加・変更（`import urllib.request`/`xml.etree` は関数内 import）:
- **新関数**: `detect_pubdate_from_pdf()`（西暦/和暦/漢数字・初版優先・OCR化けは年月へ縮退）、
  `fetch_ndl_by_isbn()`（OpenSearch 照会→ dict{title,title_kana,creators[{name,role,kana}],publisher,date,series,volume,ndc}）、
  補助 `_kanji_num`/`_pubdate_to_iso`/`_ndl_kana_title`/`_ndl_kana_name`/`_ndl_name_key`/`_ndl_parse_responsibility`、
  `_resolve_creators()`（filename/ndl 切替・NDL読み付与・異体字警告）、`_access_meta()`。
- **`build_bib_meta()` を全面刷新**: 引数を (title,title_kana,creators[],publisher,publisher_kana,pub_date,isbn,
  description,series,volume,ndc,has_text_layer) にし、dict を返す（title/creator/publisher の **file-as**、
  `dc:description`/`dc:subject`(NDC)/`dcndl:seriesTitle`+`volume`/アクセシビリティ を追加）。
- **`OPF_TMPL`**: package `prefix` に `schema:`/`dcndl:` を追加。`<dc:title>`→ id 付与＋`{title_meta}`、
  `{publisher/date/source/description/subject/series/access}_meta` プレースホルダ化。
- **`build_epub()`**: 引数追加（`author_is_cli`/`creator_source`/`use_ndl`/`detect_date`/`description`/
  `title_kana`/`author_kana`/`artist_kana`/`publisher_kana`）。冒頭で NDL 照会＋メタ解決（優先順どおり）。
- **`main()`**: `--no-ndl`/`--creator-source`/`--no-date-detect`/`--description`/`--*-kana` を追加。
- **`dc:rights` は未実装**（自動生成しない方針・手動も低優先のため今回見送り。将来 `--rights` を追加可）。

**検証（temp_sample 実機）**:
- NDL パーサ: 発行日 YYYY-MM 化、読み仮名の人物整合（Al Sweigart=無/相川愛三=アイカワ アイゾウ）、
  生年「1954-」・職業「マンガカ」除去、押見修三(ファイル名)≠押見修造(NDL) の異体字を確認。
- E2E: だもんで（NDLヒット・file-as/版元/日付/NDC/シリーズ/accessMode visual+textual）、`--no-text`（visual のみ）、
  `--no-ndl`（NDL項目なし・日付は OCR 2024-10-01）、`--creator-source ndl`（岩明均＋読み）、翻訳書（著aut＋訳trl）、
  NDL未ヒット（絶対安全剃刀→日付OCR 1982-01-19）、ISBN無し（ガラス玉→日付OCR 1976-02-29）。
- 生成 OPF は全て **XML well-formed** を確認。

---

## 6. 進捗・今後

- [x] **フェーズ1 実装（mangaP2ePub）完了**（2026-07-25）。
- [x] 検証: `normalize_isbn` ユニットテスト（接頭辞・全角・ISBN-10→13・価格コード除外・不正チェックサム）通過。
      `detect_isbn_from_pdf` を temp_sample 12冊で実測し **正解表（memory `isbn-detect-ground-truth`）と 12/12 一致**
      （検出9・非掲載3）。E2E で `--isbn`（接頭辞/ハイフン正規化）・自動検出・`--no-isbn-detect`・不正値フォールバックを確認。
- [x] README への `--isbn` 強化・`--no-isbn-detect`・自動検出の追記（表＋「ISBN の正規化と自動検出」節）。
- [x] **フェーズ2 準備・仕様確定**（2026-07-25）: 参考epub 11冊分析（§5.1-5.5）＋あらすじ/発行年月の実証（§5.6：
      あらすじ自動不可・発行年月OCR 10/12 WRONG0）＋**NDL OpenSearch 照会の実証（§5.7：読み仮名・責任表示/役割・
      発行年月・版元が取得可、カバレッジ 7/9）**＋しおりキー確認（§5.8）＋**確定仕様（§5.9）**。
- [x] **フェーズ2 実装完了**（2026-07-25、§5.10）: NDL 照会・file-as・`--creator-source`・dc:date（NDL→OCR→手動）・
      アクセシビリティ・dc:description/series/NDC。temp_sample 実機＋XML妥当性を確認。`dc:rights` のみ見送り。
- [x] README へフェーズ2 を追記（オプション表＋「NDL 書誌補完」「発行年月の自動取得」「アクセシビリティ」節）。
- [ ] jisui2epub 側は**別タスク**で独立実装（§4。今回は対象外）。
