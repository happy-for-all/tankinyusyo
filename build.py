# ============================================================
# 🌿 短期入所ナビ build.py
#
# 目的：
#   ① WAM NET CSV（csvdownload024.csv／障害福祉：短期入所）を読み込み、
#      統一スキーマのレコードに変換する
#   ② AIリサーチ結果（specialty_result.json）と事業所番号で
#      突き合わせ、医療的ケア対応等のタグを付与
#   ③ 都道府県別の軽量JSONファイルに分割して dist/ に出力
#
# 👑 設計方針（helper-navi／設計書v0.3を踏襲・2026-07-21）：
#   本ツールのデータソースは「障害福祉：短期入所」単独（フェーズ1）。
#   将来のフェーズ2で介護保険側（短期入所生活介護・短期入所療養介護）を
#   追加する可能性があるため、record構造・関数名はhelper-naviと
#   互換性を保つ設計にしてある（横展開しやすくするため）。
#
#   実データ検証（2026-07-21）で判明した重要事項：
#   ・「利用可能な時間帯」「定休日」「利用可能曜日特記事項（留意事項）」は
#     全件（9,145件）100%欠損。曜日・早朝夜間対応の判定ロジックは
#     意味をなさないため、本ツールでは実装しない（helper-naviとの違い）。
#   ・同一事業所番号で複数レコード（本所・支所等）が27件（13事業所番号分）
#     存在するため、helper-naviと同じ record_id（事業所番号＋名称・住所の
#     ハッシュ）方式を踏襲する。
#   ・事業所URL欠損39.8%のため、法人URL（法人の名称に紐づくURL）も
#     フロントで案内できるよう別途保持する。
# ============================================================


# ------------------------------------------------------------
# 1. ライブラリの読み込み
# ------------------------------------------------------------
import os
import re
import json
import shutil
import hashlib
import unicodedata
import pandas as pd


# ------------------------------------------------------------
# 2. 設定（プロジェクトに合わせて調整可能な定数）
# ------------------------------------------------------------
SHOGAI_TANKI_CSV_PATH = "csvdownload024.csv"     # WAM NET：障害福祉・短期入所
SPECIALTY_JSON_PATH = "specialty_result.json"    # AIリサーチ結果（医療的ケア対応等）
OUTPUT_DIR = "dist"

# CSVの出典表記（ダウンロード元ページの表記に合わせて手動で更新してください）
SHOGAI_TANKI_CSV_SOURCE_LABEL = "2026年3月末時点（WAM NET・障害福祉サービス等情報公表システム）"

# サービス区分バッジ（フロント表示用の固定ラベル）
SERVICE_TYPE_TANKI_SHOGAI = "disability_short_stay"
SERVICE_TYPE_TANKI_SHOGAI_LABEL = "障害福祉：短期入所"

# 都道府県名 → 出力ファイル用スラッグ（helper-naviと同一の対応表を踏襲）
ALL_PREFECTURES = {
    "北海道": "hokkaido", "青森県": "aomori", "岩手県": "iwate", "宮城県": "miyagi",
    "秋田県": "akita", "山形県": "yamagata", "福島県": "fukushima", "茨城県": "ibaraki",
    "栃木県": "tochigi", "群馬県": "gunma", "埼玉県": "saitama", "千葉県": "chiba",
    "東京都": "tokyo", "神奈川県": "kanagawa", "新潟県": "niigata", "富山県": "toyama",
    "石川県": "ishikawa", "福井県": "fukui", "山梨県": "yamanashi", "長野県": "nagano",
    "岐阜県": "gifu", "静岡県": "shizuoka", "愛知県": "aichi", "三重県": "mie",
    "滋賀県": "shiga", "京都府": "kyoto", "大阪府": "osaka", "兵庫県": "hyogo",
    "奈良県": "nara", "和歌山県": "wakayama", "鳥取県": "tottori", "島根県": "shimane",
    "岡山県": "okayama", "広島県": "hiroshima", "山口県": "yamaguchi", "徳島県": "tokushima",
    "香川県": "kagawa", "愛媛県": "ehime", "高知県": "kochi", "福岡県": "fukuoka",
    "佐賀県": "saga", "長崎県": "nagasaki", "熊本県": "kumamoto", "大分県": "oita",
    "宮崎県": "miyazaki", "鹿児島県": "kagoshima", "沖縄県": "okinawa",
}

# 障害福祉サービス等情報公表システムのCSVは「都道府県コード又は市区町村コード」
# （JIS都道府県コードの先頭2桁）から都道府県を逆引きする必要があるため、
# 標準のJIS都道府県コード順（01〜47）を定義する（helper-naviと同一）。
PREFECTURE_CODE_ORDER = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県",
    "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県",
    "福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]
PREFECTURE_CODE_MAP = {
    str(i + 1).zfill(2): name for i, name in enumerate(PREFECTURE_CODE_ORDER)
}

# 👑 AIリサーチのカテゴリ（設計書v0.3・優先順位に基づく）
# specialty_result.json が未整備でも、全カテゴリ None（＝情報なし）として
# 安全にビルドできる（helper-naviと同方式）。
SPECIALTY_CATEGORIES = [
    "medical_care",               # 優先1：医療的ケア対応
    "child_acceptance",           # 優先2：障害児対応
    "severe_behavioral_disorder", # 優先3：強度行動障害対応
    "dementia_care",              # 優先4：認知症対応
    "emergency_shortnotice",      # 優先5：緊急・即日受け入れ
]


# ------------------------------------------------------------
# 3. CSV読み込み（文字コード自動判定・helper-naviと同方式）
# ------------------------------------------------------------
def load_csv_with_encoding_fallback(path):
    """
    複数の文字コードを順に試し、読み込めたものを採用する。
    今回のファイルはutf-8-sig確認済みだが、万一の文字コード違いに
    備えて防御的な実装にしておく。
    """
    encodings_to_try = ["utf-8-sig", "utf-8", "shift_jis", "cp932"]
    last_error = None

    for enc in encodings_to_try:
        try:
            return pd.read_csv(path, encoding=enc, dtype=str)
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"CSVの読み込みに失敗しました（全ての文字コードで失敗）: {last_error}")


# ------------------------------------------------------------
# 4. 文字列・数値の安全な取得（helper-naviと同方式）
#
#    pandasはCSVの空欄を「NaN（float型）」として読み込むため、
#    そのまま str(値) とすると文字列 "nan" が入ってしまう不具合が
#    過去に発生した。全角英数字・記号はNFKCで半角に正規化する。
# ------------------------------------------------------------
def safe_str(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return text if text else None


def safe_float(value):
    """
    NaN（pandasの欠損値）を確実にNoneとして扱う、安全なfloat変換。
    float(nan) は例外を出さずに nan を返してしまい、そのまま
    json.dump すると不正な値（NaN）が出力されてしまうため、
    事前にNaN判定を行ってから変換する。
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------
# 5. 電話番号・FAX番号の正規化（helper-naviと同方式）
# ------------------------------------------------------------
def clean_phone(raw):
    """
    表示用の電話番号はそのまま活かしつつ、tel:リンク用に数字だけの
    文字列を別途生成する。
    """
    display = safe_str(raw)
    if not display:
        return None, None

    digits_only = re.sub(r"[^\d]", "", display)
    if not digits_only:
        return None, None

    return display, digits_only


# ------------------------------------------------------------
# 6. URLの補正（helper-naviと同方式）
# ------------------------------------------------------------
def build_url(raw_url):
    """
    全角文字混入・スキーム抜けを補正し、正しく開けるURLに整える。
    どうしても直せない・空欄の場合は None を返す（フロント側で
    「ホームページ情報なし」として扱われる）。
    """
    url = safe_str(raw_url)
    if not url:
        return None

    url = re.sub(r"^(https?):(?!//)", r"\1://", url)
    url = re.sub(r"^(https?)//", r"\1://", url)

    if not re.match(r"^https?://", url):
        url = "https://" + url

    return url


# ------------------------------------------------------------
# 6-1. レコードごとの一意なID生成（helper-naviと同方式）
#
#   実データ検証で判明：csvdownload024.csv には、本部＋支所・サテライト
#   構成の事業所が「同じ事業所番号」で複数レコード存在するケースが
#   27件（13事業所番号）ある。事業所番号だけをキーにすると、
#   index.html側の候補リスト機能で別の支所を追加したつもりが
#   同じ事業所番号を持つ他の支所も「追加済み」表示になってしまう
#   不具合が起きるため、事業所番号に加えて名称・住所のハッシュ値を
#   組み合わせた record_id を生成し、これを一意キーとして使う。
# ------------------------------------------------------------
def make_record_id(jigyosho_no, name, address):
    basis = f"{jigyosho_no}|{name}|{address}"
    short_hash = hashlib.md5(basis.encode("utf-8")).hexdigest()[:8]
    return f"{jigyosho_no}_{short_hash}"


# ------------------------------------------------------------
# 7. AIリサーチ結果（specialty_result.json）からのタグ付与
#    ★注意：これはAIによるホームページ内容の推定であり、断定ではない。
#    サイト側には必ず「AI調査・要確認」の免責を併記すること。
# ------------------------------------------------------------
# 👑 ログ改善（2026-07-22 外部レビューより採用）：build_specialty_tags()は
# 重複除去（dedup）前の生データ行に対して呼ばれるため、同一事業所番号の
# 行が複数あると同じ警告が複数回出力されてしまう。同一の(事業所番号,
# カテゴリ)の組み合わせについては、1回のビルド実行につき1回だけ警告を
# 出すよう、既に警告済みの組み合わせを記録しておく。
_warned_specialty_keys = set()


def build_specialty_tags(jigyosho_no, specialty_data):
    """
    事業所番号をキーにAIリサーチ結果を検索し、各カテゴリの
    status（specialized / mentioned / None）だけをフロント用に抽出する。
    未リサーチの場合は全カテゴリNoneのまま返す（＝「情報なし」として
    安全に表示される）。
    """
    tags = {cat: None for cat in SPECIALTY_CATEGORIES}

    entry = specialty_data.get(jigyosho_no)
    if not entry or entry.get("error") or not entry.get("tags"):
        return tags

    for cat in SPECIALTY_CATEGORIES:
        cat_result = entry["tags"].get(cat)

        # 1件の形式不備でビルド全体を止めないよう、辞書以外は
        # 警告を出したうえで安全に「情報なし」として扱う。
        if isinstance(cat_result, dict):
            tags[cat] = cat_result.get("status")  # "specialized" / "mentioned" / None
        elif cat_result:
            warn_key = (jigyosho_no, cat)
            if warn_key not in _warned_specialty_keys:
                _warned_specialty_keys.add(warn_key)
                print(
                    f"  警告：事業所番号{jigyosho_no}のカテゴリ「{cat}」の"
                    f"形式が不正なためスキップしました：{cat_result!r}"
                )

    return tags


# ------------------------------------------------------------
# 8. 都道府県コードからの都道府県名の逆引き（helper-naviと同方式）
# ------------------------------------------------------------
def prefecture_name_from_code(code_raw):
    code = safe_str(code_raw)
    if not code or len(code) < 2:
        return None
    return PREFECTURE_CODE_MAP.get(code[:2])


# ------------------------------------------------------------
# 9. 1事業所分のレコードを組み立てる（障害福祉：短期入所）
#
#    👑 helper-naviのbuild_shogai_record()との違い：
#    ・available_days / early_late_hint は元データが全件欠損のため
#      フィールド自体を持たない（フロント側でも曜日フィルタを廃止）
#    ・法人URL・法人電話番号を新たに保持する（事業所URL欠損時の
#      代替導線として、フロントの「法人サイトを見る」ボタン等に使う）
# ------------------------------------------------------------
def build_tanki_shogai_record(row, specialty_data):
    jigyosho_no = safe_str(row.get("事業所番号")) or ""

    tel_display, tel_clean = clean_phone(row.get("事業所電話番号"))
    fax_display, fax_clean = clean_phone(row.get("事業所FAX番号"))

    corp_tel_display, corp_tel_clean = clean_phone(row.get("法人電話番号"))

    lat = safe_float(row.get("事業所緯度"))
    lon = safe_float(row.get("事業所経度"))

    try:
        capacity = int(float(row.get("定員")))
    except (TypeError, ValueError):
        capacity = None

    city_part = safe_str(row.get("事業所住所（市区町村）")) or ""
    address_part = safe_str(row.get("事業所住所（番地以降）")) or ""
    full_address = (city_part + address_part).strip()

    prefecture = prefecture_name_from_code(row.get("都道府県コード又は市区町村コード"))
    # 市区町村名は「事業所住所（市区町村）」の先頭から都道府県名を除いた部分を採用
    city = city_part
    if prefecture and city_part.startswith(prefecture):
        city = city_part[len(prefecture):]

    record_name = safe_str(row.get("事業所の名称")) or ""

    # 👑 実データ検証（2026-07-21）：以下3列は全件欠損だが、将来データが
    # 更新され値が入る可能性に備えて、安全に読み取るだけはしておく。
    # フロント側では現時点でこれらを使った表示・フィルタは行わない。
    remarks = safe_str(row.get("利用可能曜日特記事項（留意事項）"))

    record = {
        "record_id": make_record_id(jigyosho_no, record_name, full_address),
        "jigyosho_no": jigyosho_no,
        "service_type": SERVICE_TYPE_TANKI_SHOGAI,
        "service_type_label": SERVICE_TYPE_TANKI_SHOGAI_LABEL,
        "name": record_name,
        "name_kana": safe_str(row.get("事業所の名称_かな")) or "",
        "corporation_name": safe_str(row.get("法人の名称")) or "",
        "corporation_url": build_url(row.get("法人URL")),
        "corporation_tel": corp_tel_display,
        "corporation_tel_clean": corp_tel_clean,
        "prefecture": prefecture,
        "city": city,
        "address": full_address,
        "lat": lat,
        "lon": lon,
        "tel": tel_display,
        "tel_clean": tel_clean,
        "fax": fax_display,
        "fax_clean": fax_clean,
        "url": build_url(row.get("事業所URL")),
        "capacity": capacity,
        "remarks": remarks,
        "specialty_tags": build_specialty_tags(jigyosho_no, specialty_data),
    }

    return record


# ------------------------------------------------------------
# 10. メインのビルド処理
# ------------------------------------------------------------
def main():
    print("==========================================")
    print("🌿 短期入所ナビ ビルド開始")
    print("==========================================")

    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # --- AIリサーチ結果の読み込み（存在しなくても安全に継続） ---
    if os.path.exists(SPECIALTY_JSON_PATH):
        try:
            with open(SPECIALTY_JSON_PATH, "r", encoding="utf-8") as f:
                specialty_data = json.load(f)
            print(f"AIリサーチ結果を読み込み：{len(specialty_data)}件分")
        except json.JSONDecodeError as e:
            print(
                f"警告：{SPECIALTY_JSON_PATH} の形式が不正なため、"
                f"AIタグ無しでビルドします：{e}"
            )
            specialty_data = {}
    else:
        specialty_data = {}
        print(f"警告：{SPECIALTY_JSON_PATH} が見つからないため、AIタグ無しでビルドします")

    # --- 障害福祉：短期入所 CSVの読み込み ---
    df_shogai = load_csv_with_encoding_fallback(SHOGAI_TANKI_CSV_PATH)
    print(f"短期入所CSV読み込み完了：全国{len(df_shogai)}件")

    # --- AIリサーチ済み都道府県一覧の自動導出（helper-naviと同方式）---
    researched_jigyosho_nos = set(specialty_data.keys())

    df_shogai["_jigyosho_no_normalized"] = df_shogai["事業所番号"].map(safe_str)
    df_shogai["_prefecture_derived"] = df_shogai["都道府県コード又は市区町村コード"].map(
        prefecture_name_from_code
    )
    researched_pref_names = set(
        df_shogai.loc[
            df_shogai["_jigyosho_no_normalized"].isin(researched_jigyosho_nos),
            "_prefecture_derived",
        ]
    )
    researched_prefectures = sorted(
        ALL_PREFECTURES[name] for name in researched_pref_names if name in ALL_PREFECTURES
    )

    # --- 都道府県ごとにレコードを組み立てて出力 ---
    manifest = {
        "shogai_tanki_csv_source": SHOGAI_TANKI_CSV_SOURCE_LABEL,
        "specialty_research_count": len(specialty_data),
        "specialty_researched_prefectures": researched_prefectures,
        "prefectures": {},
        "total_count": 0,
    }

    for pref_name, pref_slug in ALL_PREFECTURES.items():
        df_pref_shogai = df_shogai[df_shogai["_prefecture_derived"] == pref_name]

        records_raw = [
            build_tanki_shogai_record(row, specialty_data)
            for _, row in df_pref_shogai.iterrows()
        ]

        # 👑 バグ修正（2026-07-21 全国データ徹底検証で判明）：
        # csvdownload024.csv に、事業所番号・名称・住所が完全に一致する
        # 重複行が3件（東京都1事業所×3件、山梨県1事業所×2件）存在した。
        # これにより同一施設が検索結果に2〜3回重複表示されてしまうため、
        # record_id基準で重複を除去してから出力する（最初の1件を採用）。
        seen_record_ids = set()
        records = []
        for rec in records_raw:
            if rec["record_id"] in seen_record_ids:
                continue
            seen_record_ids.add(rec["record_id"])
            records.append(rec)

        output_path = os.path.join(OUTPUT_DIR, f"data_{pref_slug}.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)

        manifest["prefectures"][pref_slug] = {
            "name": pref_name,
            "count": len(records),
        }
        manifest["total_count"] += len(records)

        print(f"  {pref_name}（{pref_slug}）：{len(records)}件 → {output_path}")

    del df_shogai["_jigyosho_no_normalized"]
    del df_shogai["_prefecture_derived"]

    manifest_path = os.path.join(OUTPUT_DIR, "data_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # --- 静的ファイルのコピー（CF Workerのassets配信はdist/配下のみ対象）---
    static_files_to_copy = ["index.html", "favicon.ico", "ads.txt"]
    for filename in static_files_to_copy:
        if os.path.exists(filename):
            shutil.copy(filename, os.path.join(OUTPUT_DIR, filename))
            print(f"  静的ファイルをコピー：{filename} → {OUTPUT_DIR}/{filename}")
        else:
            print(f"  ⚠️ {filename} が見つからないため、コピーをスキップしました（後日追加予定）")

    print("==========================================")
    print(f"✅ ビルド完了：合計{manifest['total_count']}件")
    print(f"マニフェスト：{manifest_path}")
    print("==========================================")


# ------------------------------------------------------------
# 11. 実行
# ------------------------------------------------------------
if __name__ == "__main__":
    main()
