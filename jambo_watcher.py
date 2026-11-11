"""
Yakiniku Jambo Hongo (TableCheck) 空位監察程式

安裝:
  pip3 install playwright requests
  python3 -m playwright install chromium

先測試(只行一次,逐個時段顯示狀態,唔會發通知):
  python3 jambo_watcher.py --test

測試通知(只發一個測試訊息,唔會開瀏覽器):
  python3 jambo_watcher.py --notify-test

正式監察(每幾分鐘檢查一次,有位就通知):
  python3 jambo_watcher.py
"""
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright

# ===== 設定 =====
URL = "https://www.tablecheck.com/en/shops/yakiniku-jambo-hongou/reserve"
DATES = ["2026-12-08", "2026-12-09"]  # 想監察嘅日期
ADULTS = "2"                          # 大人人數
INTERVAL_MIN = 5                      # 幾分鐘檢查一次(本機模式)
# 手機推送:喺 ntfy app 訂閱自己諗嘅 topic 名,填喺度(或用環境變數 NTFY_TOPIC)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
# Telegram 通知(可選):填 bot token 同你嘅 chat id,或用環境變數
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# 雲端(GitHub Actions)模式:只行一次就收工
RUN_ONCE = os.environ.get("RUN_ONCE") == "1"
# ================

TEST = "--test" in sys.argv


def notify(msg: str) -> None:
    print("通知:", msg)
    if sys.platform == "darwin":
        try:
            safe = msg.replace('"', "'")
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe}" with title "Jambo 有位!" sound name "Glass"'],
                timeout=10,
            )
        except Exception as e:
            print("Mac 通知失敗:", e)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID,
                      "text": msg + "\n" + URL},
                timeout=10,
            )
            if not r.ok:
                print("Telegram 推送失敗:", r.text)
        except Exception as e:
            print("Telegram 推送失敗:", e)
    if NTFY_TOPIC:
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=msg.encode("utf-8"),
                headers={"Title": "Jambo has seats", "Priority": "urgent",
                         "Click": URL},
                timeout=10,
            )
        except Exception as e:
            print("手機推送失敗:", e)


def time_select(page):
    return page.locator("select").filter(
        has=page.locator("option", has_text="Select Time")).first


def adults_select(page):
    return page.locator("select").filter(
        has=page.locator("option", has_text="Adults")).first


def prepare(page) -> None:
    page.goto(URL, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)
    # 剔「I confirm I've read the Message from Venue」
    try:
        page.get_by_label("I confirm").first.check(force=True, timeout=3000)
    except Exception:
        pass


_debug_done = False

DATE_FIELD = "#reservation_start_date"

# 搵日曆頂部「箭咀 | 2026 December | 箭咀」呢一行
CTX_JS = r"""
const leaves = [...document.querySelectorAll('*')].filter(
  e => e.children.length === 0 && /^\s*\d{4}\s+[A-Za-z]+\s*$/.test(e.textContent));
let ctx = null;
for (const h of leaves) {
  let n = h;
  while (n.parentElement && n.parentElement.children.length < 3) n = n.parentElement;
  const row = n.parentElement;
  if (row && row.children.length >= 3 && row.children.length <= 5) { ctx = {h, row}; break; }
}
"""

HEADER_JS = "() => {" + CTX_JS + "return ctx ? ctx.h : null; }"

NAV_JS = ("(dir) => {" + CTX_JS +
          "if (!ctx) return null;"
          "return dir === 'next' ? ctx.row.lastElementChild : ctx.row.firstElementChild; }")

DAY_CELL_JS = ("(day) => {" + CTX_JS + r"""
  if (!ctx) return null;
  const pr = ctx.row.firstElementChild.getBoundingClientRect();
  const nr = ctx.row.lastElementChild.getBoundingClientRect();
  const L = pr.left - 40, R = nr.right + 40;
  let root = ctx.row;
  while (root.parentElement) {
    const t = root.textContent;
    if (t.includes('Su') && t.includes('Sa') && t.includes('28')) break;
    root = root.parentElement;
  }
  const cells = [...root.querySelectorAll('*')].filter(
    e => e.children.length === 0 && e.tagName !== 'OPTION' && e.textContent.trim() === day
  ).filter(e => {
    const r = e.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    if (r.width === 0 || cx < L || cx > R) return false;
    let n = e;
    while (n && n !== root) {
      if (/outer/i.test(String(n.className))) return false;
      n = n.parentElement;
    }
    return true;
  });
  return cells[0] || null;
}""")

DEBUG_JS = ("() => {" + CTX_JS + r"""
  const out = {};
  const f = document.getElementById('reservation_start_date');
  out.field = f ? f.outerHTML.slice(0, 300) + ' | value=' + f.value : 'none';
  if (ctx) {
    let root = ctx.row;
    for (let i = 0; i < 3 && root.parentElement; i++) root = root.parentElement;
    out.calendar = root.outerHTML.slice(0, 2500);
  }
  return out;
}""")

DIRECT_SET_JS = r"""(d) => {
  const el = document.getElementById('reservation_start_date');
  if (!el) return false;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  setter.call(el, d);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  if (window.jQuery) { window.jQuery(el).trigger('change'); }
  return true;
}"""


def dump_debug(page) -> None:
    global _debug_done
    if _debug_done:
        return
    _debug_done = True
    try:
        info = page.evaluate(DEBUG_JS)
        print("---- 偵錯資料(請成段貼俾 Claude)----")
        print("field:", info.get("field"))
        print("calendar:", info.get("calendar"))
        print("---- 偵錯資料完 ----")
    except Exception as e:
        print("偵錯資料讀取失敗:", e)


def pick_by_calendar(page, d: str) -> None:
    target = datetime.strptime(d, "%Y-%m-%d")
    day = str(target.day)
    page.locator(DATE_FIELD).click()
    page.wait_for_timeout(1000)
    for _ in range(14):
        h = page.evaluate_handle(HEADER_JS).as_element()
        if h is None:
            raise RuntimeError("搵唔到日曆標題")
        cur = datetime.strptime(h.inner_text().strip(), "%Y %B")
        diff = (target.year - cur.year) * 12 + (target.month - cur.month)
        if diff == 0:
            break
        arrow = page.evaluate_handle(NAV_JS, "next" if diff > 0 else "prev").as_element()
        if arrow is None:
            raise RuntimeError("搵唔到月份箭咀")
        arrow.click()
        page.wait_for_timeout(900)
    else:
        raise RuntimeError("翻唔到目標月份")
    cell = page.evaluate_handle(DAY_CELL_JS, day).as_element()
    if cell is None:
        raise RuntimeError("搵唔到日子格")
    cell.click()
    page.wait_for_timeout(1500)
    val = page.locator(DATE_FIELD).input_value()
    if val != d:
        raise RuntimeError("日期欄顯示 " + val)


def set_date(page, d: str) -> bool:
    try:
        pick_by_calendar(page, d)
        return True
    except Exception as e:
        print("日曆點擊失敗(" + str(e) + "),改用直接設定")
        dump_debug(page)
    try:
        page.keyboard.press("Escape")
        page.evaluate(DIRECT_SET_JS, d)
        page.wait_for_timeout(2000)
        return page.locator(DATE_FIELD).input_value() == d
    except Exception as e:
        print("直接設定失敗:", e)
        return False


def slot_status(page, d: str, label: str):
    """回傳 (狀態, 說明)。狀態: full / open? / unknown"""
    time_select(page).select_option(label=label)
    want_date = datetime.strptime(d, "%Y-%m-%d").strftime("%B %d, %Y")
    for wait_ms in (2500, 3000):
        page.wait_for_timeout(wait_ms)
        body = page.inner_text("body")
        m = re.search(r"No table is available[^\n]*", body)
        if m:
            if want_date in m.group(0):
                return "full", ""
            return "unknown", "頁面顯示嘅日期唔一樣: " + m.group(0)
    return "open?", ""


def one_pass(browser):
    results = []
    page = browser.new_page(viewport={"width": 1280, "height": 1800})
    try:
        prepare(page)
        for d in DATES:
            if not set_date(page, d):
                results.append((d, "-", "unknown", "日期設定失敗(見上面訊息)"))
                continue
            try:
                adults_select(page).select_option(label=ADULTS)
            except Exception as e:
                results.append((d, "-", "unknown", f"設定人數失敗: {e}"))
                continue
            page.wait_for_timeout(1000)
            labels = [t.strip() for t in
                      time_select(page).locator("option").all_inner_texts()
                      if "Select" not in t]
            if not labels:
                results.append((d, "-", "full", "當日冇任何時段"))
                continue
            for label in labels:
                status, note = slot_status(page, d, label)
                results.append((d, label, status, note))
    finally:
        page.close()
    return results


def main() -> None:
    if "--notify-test" in sys.argv:
        notify("測試通知:如果你收到呢個訊息,通知設定成功!")
        return
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        while True:
            stamp = time.strftime("%H:%M:%S")
            try:
                results = one_pass(browser)
            except Exception as e:
                print(f"[{stamp}] 檢查出錯:", e)
                results = []

            for d, label, status, note in results:
                print(f"[{stamp}] {d} {label:>8}  {status}  {note}")

            open_slots = [f"{d} {label}" for d, label, s, _ in results
                          if s == "open?"]
            if TEST:
                print("\n測試完成。全部應該顯示 full;如果有 unknown,"
                      "請將上面輸出貼俾 Claude。")
                break
            if open_slots:
                notify("可能有位: " + ", ".join(open_slots) + " 快啲去 book!")
            if any(s == "unknown" for _, _, s, _ in results):
                print("有時段偵測唔到狀態,請留意輸出。")
            if RUN_ONCE:
                break
            time.sleep(INTERVAL_MIN * 60 + random.randint(0, 60))
        browser.close()


if __name__ == "__main__":
    main()
