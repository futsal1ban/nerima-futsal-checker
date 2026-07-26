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

【空き判定の仕組み】
DevToolsで確認していただいた実際のHTML構造に基づき、
<td id="YYYY/MM/DD">セル内に <span class="vacant">予約申込可能</span> が
あるかどうかで判定しています（build_day_results 関数）。
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
    {"key": "sunlife", "name": "サンライフ練馬", "facility_id": 201},
    {"key": "kasugacho", "name": "春日町青少年館", "facility_id": 36},
]

# 今日から何か月分見るか
MONTHS_AHEAD = 4

# 「この時間帯だけしか空いていない場合は、空きなし扱いにする」時間帯
# (開始時刻, 終了時刻) のタプルで指定。表記ゆれ（21:0 など）は正規化して比較します。
EXCLUDED_TIME_SLOTS = {("21:00", "21:30")}

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


def fetch_month(page, facility_id: int, facility_name: str, year_month: str, debug_name: str):
    """
    指定した施設・月のページを開いて、
    ・「検索する」ボタンをクリックして実際の空き状況を表示させる
    ・取得できた結果が本当に狙った施設のものか確認する（違えば作り直す）
    ・スクリーンショット(debug/xxx.png)
    ・画面のテキスト全部(debug/xxx.txt)
    ・その月の全日付と空き有無(debug/xxx_day_status.json)
    を保存する。戻り値は {日にち(int): 空きあり(True)/なし(False)} の辞書。

    【背景】
    以前、同じ画面(page)を使い回して施設を連続して切り替えると、
    サイト側の内部状態が前の施設のまま残ってしまい、
    「春日町のはずがサンライフ練馬の結果が返ってくる」ことがありました。
    そのため、ここで「本当にその施設の結果になっているか」を毎回確認しています。
    """
    url = build_url(facility_id, year_month)

    for attempt in range(1, 4):  # 最大3回まで試す
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(3000)

        try:
            page.get_by_text("検索する", exact=True).click(timeout=10000)
        except Exception as e:
            print(f"[警告] 「検索する」ボタンのクリックに失敗しました: {e}")

        # クリック直後は、データが届く前の「一時的な空表示」を
        # 「本当に空きがない」と誤判定してしまうことがあるため、
        # ・最初の4秒は判定せずに待つ
        # ・「見つかりませんでした」は連続2回(1秒以上)観測できてから確定する
        # ・カレンダー表が実際に描画されたら、その場で確定してよい
        page.wait_for_timeout(4000)

        body_text = ""
        facility_ok = False
        calendar_rendered = False
        no_result_streak = 0
        confirmed = False
        for _ in range(20):  # 1秒 x 20回 = 最大20秒
            body_text = page.inner_text("body")
            facility_ok = facility_name in body_text
            try:
                calendar_rendered = page.locator("#calendar table tbody tr").count() > 0
            except Exception:
                calendar_rendered = False

            if facility_ok and calendar_rendered:
                confirmed = True
                break

            if facility_ok and "見つかりませんでした" in body_text:
                no_result_streak += 1
            else:
                no_result_streak = 0

            if facility_ok and no_result_streak >= 2:
                confirmed = True
                break

            page.wait_for_timeout(3000)

        if confirmed:
            break

        if not facility_ok:
            print(
                f"[警告] {attempt}回目: 期待した施設「{facility_name}」と異なる結果が"
                f"表示されたため、ページを開き直します。"
            )
        else:
            print(
                f"[警告] {attempt}回目: 10秒待ってもカレンダーの描画が確認できなかったため、"
                f"ページを開き直します。"
            )
    else:
        print(f"[エラー] {facility_name} {year_month}: 3回試しても正しい結果を取得できませんでした。")

    os.makedirs(DEBUG_DIR, exist_ok=True)
    screenshot_path = os.path.join(DEBUG_DIR, f"{debug_name}.png")
    text_path = os.path.join(DEBUG_DIR, f"{debug_name}.txt")
    day_status_path = os.path.join(DEBUG_DIR, f"{debug_name}_day_status.json")

    page.screenshot(path=screenshot_path, full_page=True)
    with open(text_path, "w", encoding="utf-8") as f:
        f.write(body_text)

    day_status = get_calendar_day_status(page)
    with open(day_status_path, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in sorted(day_status.items())}, f, ensure_ascii=False, indent=2)

    # ★調査用★
    # 1件も日付セルを検出できなかった場合（想定外のHTML構造だった場合）、
    # 画面全体のHTMLを保存しておく。これを見れば正確な原因調査ができる。
    if not day_status:
        html_debug_path = os.path.join(DEBUG_DIR, f"{debug_name}_html_sample.txt")
        try:
            html = page.evaluate("() => document.getElementById('calendar')?.outerHTML || document.body.innerHTML")
            with open(html_debug_path, "w", encoding="utf-8") as f:
                f.write(html[:20000])
        except Exception as e:
            print(f"[警告] HTML構造の調査保存に失敗しました: {e}")

    return day_status


# ============================================================
# 空き状況の読み取り
# ============================================================
# DevToolsで確認していただいた実際のHTML構造(<td id="YYYY/MM/DD">の中に
# <span class="vacant">予約申込可能</span>があるかどうか)に基づいて判定しています。

def get_calendar_day_status(page):
    """
    ページ内の各日付セル <td id="YYYY/MM/DD"> をすべて調べ、
    「その日が存在するか」「空き(<span class="vacant">)があるか」を一度に取得する。
    （＝実際にDevToolsで確認していただいたHTML構造に基づく、確実な判定方法）

    戻り値: {日にち(int): True/False}
        True  = その日に空きがある（<span class="vacant">が存在する）
        False = その日は表示されているが空きがない
    （out-of-month = 前後の月にはみ出た日付セルにはidが付かないため、
      自動的にこの月の日付だけが対象になる）
    """
    try:
        raw = page.evaluate(
            """
            () => {
              const out = {};
              document.querySelectorAll('td[id]').forEach(td => {
                const parts = td.id.split('/');
                if (parts.length === 3) {
                  const day = parseInt(parts[2], 10);
                  out[day] = !!td.querySelector('span.vacant');
                }
              });
              return out;
            }
            """
        )
    except Exception as e:
        print(f"[警告] カレンダーセルの検出に失敗しました: {e}")
        return {}

    return {int(k): v for k, v in raw.items()}


# 「21:00〜21:30」「21:00~21:30」「21:00-21:30」のような時間帯表記を探す
TIME_RANGE_PATTERN = re.compile(r"(\d{1,2}:\d{2})\s*[〜~\-–]\s*(\d{1,2}:\d{2})")


def normalize_time(t: str) -> str:
    h, m = t.split(":")
    return f"{int(h):02d}:{m}"


def get_available_time_ranges(page):
    """
    現在表示されているモーダル（日付クリック後に開く時間帯選択パネル）の中から、
    空いている時間帯をすべて取得する。

    実際のHTML構造（DevToolsで確認していただいたもの）:
      <div class="modal_timelist1">
        <label for="...">21:00〜21:30</label>
      </div>
    という形で、時間帯は<label>タグのテキストとして入っている。
    （ボタンやリンクではないので、以前の検出方法では見つからなかった）
    """
    ranges = []
    try:
        candidates = page.locator(".ant-modal-content .modal_timelist1 label")
        count = candidates.count()
    except Exception:
        return ranges

    for i in range(count):
        try:
            txt = candidates.nth(i).inner_text(timeout=1000).strip()
        except Exception:
            continue
        m = TIME_RANGE_PATTERN.fullmatch(txt.replace(" ", ""))
        if m:
            ranges.append((normalize_time(m.group(1)), normalize_time(m.group(2))))
    return ranges


def close_day_detail(page):
    """
    日付をクリックして開いた時間帯の詳細パネル（モーダル）を閉じる。
    実際のHTML構造では、右上の「×」ボタン(aria-label="Close")と
    下部の「キャンセルする」ボタンの両方が存在する。
    """
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:
        pass
    for selector in [
        "button[aria-label='Close']",
        "button:has-text('キャンセルする')",
        "button:has-text('閉じる')",
        "button:has-text('×')",
    ]:
        try:
            el = page.locator(selector).first
            if el.count() > 0:
                el.click(timeout=1000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def refine_days_with_time_slot_filter(page, year: int, month: int, day_numbers):
    """
    day_numbers（「クリック可能＝空きの可能性あり」と判定された日）を1つずつクリックし、
    EXCLUDED_TIME_SLOTS 以外に空き時間帯が1つでもあるかを確認する。

    戻り値: {日にち(int): True/False}
        True  = 除外時間帯以外にも空きがある（＝通知対象）
        False = 除外時間帯（21:00〜21:30など）しか空いていない（＝通知しない）

    ※ここは実際の画面構造を見ずに書いているため、うまく動かない場合があります。
      その場合は取得に失敗した日を「わからないのでTrue扱い（今まで通り通知する）」
      にフォールバックするので、見落としが増える方向の失敗になります。
    """
    result = {}
    debug_html_saved = False
    for day in sorted(day_numbers):
        date_id = f"{year}/{month:02d}/{day:02d}"
        try:
            day_locator = page.locator(f'td[id="{date_id}"] span.vacant').first
            day_locator.click(timeout=5000)
            page.wait_for_timeout(2500)  # モーダルのアニメーション等を考慮して少し長めに待つ

            ranges = get_available_time_ranges(page)

            # ★調査用★ 時間帯が1件も取れなかった場合、最初の1回だけ
            # ポップアップ(モーダル/ドロワー)部分のHTMLをデバッグ保存しておく（原因調査用）。
            # このサイトはAnt Designという部品を使っており、
            # ポップアップの中身は画面の見た目とは離れた場所(bodyの末尾付近)に
            # 追加されることが多いため、それらしき要素を優先して探す。
            if not ranges and not debug_html_saved:
                debug_html_saved = True
                try:
                    html = page.evaluate(
                        """
                        () => {
                          const selectors = [
                            '.ant-modal-content', '.ant-drawer-content',
                            '.ant-modal-body', '.ant-drawer-body',
                            '[class*="modal"]', '[class*="drawer"]', '[class*="popup"]'
                          ];
                          for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el) return `[${sel} で発見]\\n` + el.outerHTML;
                          }
                          return '[それらしき要素が見つからなかったため、body全体の末尾3万文字]\\n'
                            + document.body.innerHTML.slice(-30000);
                        }
                        """
                    )
                    path = os.path.join(DEBUG_DIR, f"slot_detail_html_sample_{year}-{month:02d}-{day:02d}.txt")
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(html[:30000])
                except Exception as e:
                    print(f"[警告] 時間帯詳細のHTML保存に失敗しました: {e}")

            remaining = [r for r in ranges if r not in EXCLUDED_TIME_SLOTS]
            result[day] = len(remaining) > 0 if ranges else True

            close_day_detail(page)
        except Exception as e:
            print(f"[警告] {year}/{month:02d} {day}日の時間帯詳細取得に失敗しました: {e}")
            result[day] = True  # わからない場合は従来通り「空きあり」扱いにしておく

    return result


def build_day_results(year_month: str, day_status: dict, refined_days=None):
    """
    「日付 → 状態」の辞書を作る。
    ・day_status で「空きなし(False)」だった日 → 空きなし(推定)
    ・空きあり(True)で、時間帯詳細を確認できた日 → 除外時間帯以外に空きがあれば「空きあり」、無ければ「空きなし(21時枠のみ)」
    ・空きあり(True)で、時間帯詳細を未確認の日   → 「空きあり」（従来通り。平日は詳細確認をしないためここに該当）
    """
    refined_days = refined_days or {}
    year, month = map(int, year_month.split("/"))
    results = {}

    for day, is_vacant in day_status.items():
        d = date(year, month, day)
        if not is_vacant:
            status = "空きなし(推定)"
        elif day in refined_days:
            status = "空きあり" if refined_days[day] else "空きなし(21時枠のみ)"
        else:
            status = "空きあり"
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

        for facility in FACILITIES:
            fkey = facility["key"]
            current_state[fkey] = {}

            # 施設が変わるたびに、まっさらな状態のブラウザ画面を新しく開く。
            # (前の施設の情報が残ったまま次の施設を検索してしまうバグの対策)
            context = browser.new_context()
            page = context.new_page()

            for ym in year_months:
                debug_name = f"{fkey}_{ym.replace('/', '-')}"
                day_status = fetch_month(
                    page, facility["facility_id"], facility["name"], ym, debug_name
                )
                year, month = map(int, ym.split("/"))

                # 土日祝かつ空きの可能性がある日だけ、
                # 時間帯の詳細まで確認しにいく（対象が少ないので現実的な処理時間で収まる）
                target_days = {
                    d for d, is_vacant in day_status.items()
                    if is_vacant and is_weekend_or_holiday(date(year, month, d))
                }
                refined = refine_days_with_time_slot_filter(page, year, month, target_days)

                # 判定結果をデバッグ用に保存（あとで見比べられるように）
                debug_slot_path = os.path.join(DEBUG_DIR, f"{debug_name}_slot_check.json")
                with open(debug_slot_path, "w", encoding="utf-8") as f:
                    json.dump({str(k): v for k, v in refined.items()}, f, ensure_ascii=False, indent=2)

                parsed = build_day_results(ym, day_status, refined)

                for iso_date, status in parsed.items():
                    d = date.fromisoformat(iso_date)
                    if is_weekend_or_holiday(d):
                        current_state[fkey][iso_date] = status

            context.close()  # この施設の確認が終わったら画面を閉じる（次の施設はまっさらな画面で開始）

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
