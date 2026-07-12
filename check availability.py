# -*- coding: utf-8 -*-
"""
練馬区施設予約システム 空き状況チェッカー
=====================================

【これは何をするプログラム？】
サンライフ練馬・春日町青年館の「今日から3か月分」の予約カレンダーを開き、
土日祝日に空きがあるかどうかを自動でチェックします。

・1回目の実行（state/availability.json がまだ無いとき）
    → 見つかった土日祝日の状況を「全部」LINEに送ります。（答え合わせ用）
・2回目以降の実行
    → 前回チェック時と比べて「新しく空きになった日」があるときだけLINEに送ります。

【重要な注意】
このサイトはJavaScriptで画面を作るタイプのサイト（SPA）なので、
「日付の横にどんな記号（○/△/×など）でどう空き状況が書かれているか」を
作者（Claude）は実際の画面で確認できていません。
そのため、空き状況の読み取り（parse_calendar_text 関数）は「たぶんこうだろう」という
推測で書いています。1回目の実行結果と、debug/ フォルダに保存されるスクリーンショット・
テキストを見比べて、もし読み取りがおかしければ一緒に調整しましょう。
"""

import json
import os
import re
import sys
from datetime import date

import jpholiday
import requests
from playwright.sync_api import sync_playwright

# ============================================================
# 設定（ここを見れば全体の設定がわかるようにまとめています）
# ============================================================

# 練馬区施設予約システムの共通パラメータ
GROUP = 25989
USE_TYPE = 150070
BASE_URL = "https://www.shisetsuyoyaku.city.nerima.tokyo.jp/reservation/search"

# チェックしたい施設（facility_id は施設ごとに割り振られたID）
FACILITIES = [
    {"key": "sunlife", "name": "サンライフ練馬", "facility_id": 36},
    {"key": "kasugacho", "name": "春日町青年館", "facility_id": 201},
]

# 今日から何か月分見るか
MONTHS_AHEAD = 3

# 状態保存ファイル・デバッグ用フォルダ
STATE_PATH = "state/availability.json"
DEBUG_DIR = "debug"

# LINEの設定（GitHub Secretsから読み込みます。詳しくはREADME参照）
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")


# ============================================================
# 日付関連のヘルパー
# ============================================================

def get_target_year_months(n=MONTHS_AHEAD):
    """今日を含めて n か月分の 'YYYY/MM' 文字列リストを作る"""
    today = date.today()
    result = []
    y, m = today.year, today.month
    for i in range(n):
        total = (m - 1) + i
        yy = y + total // 12
        mm = total % 12 + 1
        result.append(f"{yy}/{mm:02d}")
    return result


def is_weekend_or_holiday(d: date) -> bool:
    """土日、または日本の祝日ならTrue"""
    return d.weekday() >= 5 or jpholiday.is_holiday(d)


WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


def format_date_jp(d: date) -> str:
    return f"{d.month}/{d.day}({WEEKDAY_JP[d.weekday()]})"


# ============================================================
# ページ取得
# ============================================================

def build_url(facility_id: int, year_month: str) -> str:
    ym_encoded = year_month.replace("/", "%2F")
    return (
        f"{BASE_URL}?group={GROUP}&useType={USE_TYPE}"
        f"&facility={facility_id}&usageMonth={ym_encoded}"
    )


def fetch_month(page, facility_id: int, year_month: str, debug_name: str):
    """
    指定した施設・月のページを開いて、
    ・スクリーンショット(debug/xxx.png)
    ・画面のテキスト全部(debug/xxx.txt)
    ・クリック可能と判定した日付一覧(debug/xxx_clickable.json)
    を保存する。戻り値は (画面テキスト, クリック可能な日付の集合)。
    """
    url = build_url(facility_id, year_month)
    page.goto(url, wait_until="networkidle", timeout=45000)
    # SPAが描画し終わるまで少し待つ（ここは環境によって調整が必要な場合あり）
    page.wait_for_timeout(3000)

    os.makedirs(DEBUG_DIR, exist_ok=True)
    screenshot_path = os.path.join(DEBUG_DIR, f"{debug_name}.png")
    text_path = os.path.join(DEBUG_DIR, f"{debug_name}.txt")
    clickable_path = os.path.join(DEBUG_DIR, f"{debug_name}_clickable.json")

    page.screenshot(path=screenshot_path, full_page=True)
    body_text = page.inner_text("body")
    with open(text_path, "w", encoding="utf-8") as f:
        f.write(body_text)

    clickable_days = extract_clickable_days(page)
    with open(clickable_path, "w", encoding="utf-8") as f:
        json.dump(sorted(clickable_days), f, ensure_ascii=False)

    return body_text, clickable_days


# ============================================================
# 空き状況の読み取り（★ここが「推測」で書いている部分です★）
# ============================================================
#
# 教えていただいた仕様:
#   ・空きがある日は「クリックできる表示」になっていて、
#     クリックすると空き時間が表示される
#   ・つまり「クリックできる要素になっているかどうか」＝「空きがあるかどうか」
#
# という前提で、記号（○×）を探すのではなく、
# 「クリック可能な要素（リンク/ボタン）のうち、中身が日付の数字だけのもの」
# を空きありの日として扱うロジックにしています。
# ただし実際のHTML構造は見えていないため、これも推測です。
# デバッグ用に「テキスト全体」と「クリック可能と判定した日付一覧」の両方を
# 保存するので、初回実行後にズレがあれば教えてください。

DATE_TOKEN_PATTERN = re.compile(
    r"(\d{1,2})\s*[\(（]\s*([月火水木金土日])\s*[\)）]"
)


def extract_clickable_days(page):
    """
    ページ内の「クリックできそうな要素」(a, button, role=button) のうち、
    表示テキストが「1〜31の数字だけ」のものを探し、その日付の集合を返す。
    （＝クリックすると空き時間が表示される日、という想定）
    """
    days = set()
    try:
        candidates = page.locator("a, button, [role='button']")
        count = candidates.count()
    except Exception:
        return days

    for i in range(count):
        try:
            txt = candidates.nth(i).inner_text(timeout=1000).strip()
        except Exception:
            continue
        if re.fullmatch(r"([1-9]|[12]\d|3[01])", txt):
            days.add(int(txt))
    return days


def list_all_calendar_days(body_text: str, year_month: str):
    """
    画面テキストの中から「◯(月火水木金土日)」形式の日付表記をすべて拾い、
    その月に実在する日付だけを返す（＝カレンダーに表示されている日の一覧）。
    """
    year, month = map(int, year_month.split("/"))
    all_days = set()
    for m in DATE_TOKEN_PATTERN.finditer(body_text):
        day = int(m.group(1))
        try:
            date(year, month, day)
            all_days.add(day)
        except ValueError:
            continue
    return sorted(all_days)


def parse_calendar_text(body_text: str, year_month: str, clickable_days: set):
    """
    「日付 → 状態」の辞書を作る。
    クリック可能な日として検出できた日は「空きあり」、
    カレンダー上に表示されているが未クリックの日は「空きなし(推定)」とする。
    """
    year, month = map(int, year_month.split("/"))
    results = {}

    all_days = list_all_calendar_days(body_text, year_month)
    for day in all_days:
        d = date(year, month, day)
        status = "空きあり" if day in clickable_days else "空きなし(推定)"
        results[d.isoformat()] = status

    return results


# ============================================================
# LINE通知（Messaging API / ブロードキャスト配信）
# ============================================================

def send_line_broadcast(text: str):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("[警告] LINE_CHANNEL_ACCESS_TOKEN が設定されていないため、LINE通知はスキップします。")
        print("---- 送信予定だった内容 ----")
        print(text)
        return

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    # LINEは1メッセージ5000文字までなので、念のため切る
    text = text[:4900]
    body = {"messages": [{"type": "text", "text": text}]}

    res = requests.post(url, headers=headers, json=body, timeout=15)
    if res.status_code != 200:
        print(f"[エラー] LINE送信に失敗しました: {res.status_code} {res.text}")
    else:
        print("[OK] LINE通知を送信しました。")


# ============================================================
# 状態の保存・読み込み
# ============================================================

def load_previous_state():
    if not os.path.exists(STATE_PATH):
        return None
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
# メイン処理
# ============================================================

def main():
    year_months = get_target_year_months()
    print(f"チェック対象月: {year_months}")

    # 施設ごとの結果を格納: current_state["sunlife"]["2026-09-06"] = "空きあり"
    current_state = {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        for facility in FACILITIES:
            fkey = facility["key"]
            current_state[fkey] = {}

            for ym in year_months:
                debug_name = f"{fkey}_{ym.replace('/', '-')}"
                body_text, clickable_days = fetch_month(
                    page, facility["facility_id"], ym, debug_name
                )
                parsed = parse_calendar_text(body_text, ym, clickable_days)

                for iso_date, status in parsed.items():
                    d = date.fromisoformat(iso_date)
                    if is_weekend_or_holiday(d):
                        current_state[fkey][iso_date] = status

        browser.close()

    previous_state = load_previous_state()

    if previous_state is None:
        # ---- 1回目の実行：全結果を報告する ----
        lines = ["【初回チェック結果】土日祝の空き状況（答え合わせ用）"]
        for facility in FACILITIES:
            fkey = facility["key"]
            lines.append(f"\n■{facility['name']}")
            dates = sorted(current_state.get(fkey, {}).keys())
            if not dates:
                lines.append("（対象期間の日付を読み取れませんでした。debugフォルダを確認してください）")
                continue
            for iso_date in dates:
                d = date.fromisoformat(iso_date)
                status = current_state[fkey][iso_date]
                lines.append(f"{format_date_jp(d)}: {status}")

        lines.append(
            "\n※この判定は自動読み取りによる推測です。"
            "debugフォルダのスクリーンショットと見比べて、"
            "実際の空き状況と合っているか確認してください。"
        )
        message = "\n".join(lines)
        print(message)
        send_line_broadcast(message)

    else:
        # ---- 2回目以降：新しく「空きあり」になった日だけ報告 ----
        new_available = []
        for facility in FACILITIES:
            fkey = facility["key"]
            prev = previous_state.get(fkey, {})
            curr = current_state.get(fkey, {})
            for iso_date, status in curr.items():
                prev_status = prev.get(iso_date, "不明")
                if status == "空きあり" and prev_status != "空きあり":
                    d = date.fromisoformat(iso_date)
                    new_available.append(f"{facility['name']} {format_date_jp(d)}")

        if new_available:
            message = "【新しく空きが出ました】\n" + "\n".join(new_available)
            print(message)
            send_line_broadcast(message)
        else:
            print("新しい空きはありませんでした。（通知は送信しません）")

    save_state(current_state)


if __name__ == "__main__":
    main()
