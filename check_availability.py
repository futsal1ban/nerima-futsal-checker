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
このサイトはJavaScriptで画面を作るタイプのサイト（SPA）です。
2026/07/13 に実際のページのHTML構造を確認できたので、現在は
「<td id="YYYY/MM/DD">要素の中に<span class="vacant">があるかどうか」で
確実に空き状況を判定しています（extract_availability関数）。
デバッグ用に、毎回スクリーンショット・画面テキスト・カレンダーHTML・
判定結果(JSON)をdebugフォルダに保存するので、おかしな点があれば
見比べて調整しましょう。
"""

import json
import os
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
# ※2026/07/13 実際のサイトで確認した結果、facility_idが逆だったため修正
#   （facility_id=36 → 春日町青少年館 / facility_id=201 → サンライフ練馬）
FACILITIES = [
    {"key": "sunlife", "name": "サンライフ練馬", "facility_id": 201},
    {"key": "kasugacho", "name": "春日町青少年館", "facility_id": 36},
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
    ・「検索する」ボタンをクリックして実際の空き状況を表示させる
    ・スクリーンショット(debug/xxx.png)
    ・画面のテキスト全部(debug/xxx.txt)
    ・カレンダーテーブルのHTML(debug/xxx_calendar.html)
    ・日付ごとの空き状況(debug/xxx_availability.json)
    を保存する。戻り値は {"2026-07-18": True, ...} という
    「日付(ISO形式) → 空きありかどうか」の辞書。
    """
    url = build_url(facility_id, year_month)
    page.goto(url, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(1500)

    # URLのパラメータだけでは検索結果(カレンダー)が表示されず、
    # 画面上の「検索する」ボタンを押して初めて空き状況が表示される仕様のため、
    # ボタンを探してクリックする。
    try:
        page.get_by_text("検索する", exact=True).click(timeout=10000)
        page.wait_for_timeout(3000)
        # 空きアイコンが非同期で後から表示されるケースがあるため、
        # ネットワークが落ち着くまで少し追加で待つ
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"[警告] 「検索する」ボタンのクリックに失敗しました: {e}")

    os.makedirs(DEBUG_DIR, exist_ok=True)
    screenshot_path = os.path.join(DEBUG_DIR, f"{debug_name}.png")
    text_path = os.path.join(DEBUG_DIR, f"{debug_name}.txt")
    html_path = os.path.join(DEBUG_DIR, f"{debug_name}_calendar.html")
    availability_path = os.path.join(DEBUG_DIR, f"{debug_name}_availability.json")

    page.screenshot(path=screenshot_path, full_page=True)
    body_text = page.inner_text("body")
    with open(text_path, "w", encoding="utf-8") as f:
        f.write(body_text)

    # カレンダー部分のHTML（デバッグ用に毎回保存しておく）
    try:
        table_count = page.locator("table").count()
        if table_count > 0:
            calendar_html = page.locator("table").first.evaluate("el => el.outerHTML")
        else:
            calendar_html = page.evaluate("document.body.outerHTML")
    except Exception as e:
        calendar_html = f"<!-- HTML取得に失敗しました: {e} -->"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(calendar_html)

    availability = extract_availability(page)
    with open(availability_path, "w", encoding="utf-8") as f:
        json.dump(availability, f, ensure_ascii=False, indent=2)

    return availability


# ============================================================
# 空き状況の読み取り
# ============================================================
#
# 2026/07/13 に実際のページのHTML構造を確認できました。
# カレンダーは以下のような構造になっています:
#
#   <td id="2026/07/18" class="saturday" style="cursor: pointer;">
#     <div>18</div>
#     <span role="button" tabindex="0" class="vacant">予約申込可能</span>
#   </td>
#
#   ・日付セルは <td id="YYYY/MM/DD"> という形式
#   ・空きがある日だけ、中に <span class="vacant">予約申込可能</span> が追加される
#   ・空きがない日は <div>日付</div> だけで span.vacant はない
#   ・対象月外の日（前後月のグレー表示分）は id 属性が付かない
#
# これに基づき、○×などの記号を画面テキストから探すのではなく、
# 「td[id] 要素の中に span.vacant があるかどうか」を直接判定します。


def extract_availability(page):
    """
    カレンダーの <td id="YYYY/MM/DD"> 要素をすべて取得し、
    中に <span class="vacant"> があるかどうかで空き状況を判定する。
    戻り値: {"2026-07-18": True, "2026-07-19": False, ...}
    （キーはISO形式の日付文字列、値はTrue=空きあり/False=空きなし）
    """
    try:
        raw = page.evaluate(
            """
            () => {
                const result = {};
                document.querySelectorAll('td[id]').forEach(td => {
                    result[td.id] = td.querySelector('span.vacant') !== null;
                });
                return result;
            }
            """
        )
    except Exception as e:
        print(f"[警告] 空き状況の取得に失敗しました: {e}")
        return {}

    # td の id は "2026/07/18" 形式なので "2026-07-18"(ISO形式) に変換
    availability = {}
    for raw_id, is_vacant in raw.items():
        try:
            y, m, d = raw_id.split("/")
            iso_date = date(int(y), int(m), int(d)).isoformat()
        except Exception:
            continue
        availability[iso_date] = bool(is_vacant)
    return availability


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
                availability = fetch_month(
                    page, facility["facility_id"], ym, debug_name
                )

                for iso_date, is_vacant in availability.items():
                    d = date.fromisoformat(iso_date)
                    if is_weekend_or_holiday(d):
                        current_state[fkey][iso_date] = "空きあり" if is_vacant else "空きなし"

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
