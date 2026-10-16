import csv
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import openpyxl
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

HERE = Path(__file__).resolve().parent
PLACES_XLSX = HERE.parent / "DOHMH_New_York_City_Restaurant_Inspection_Results_20260923_with_FTID.xlsx"
FIRST_ROW, LAST_ROW = 118, 234   # Excel rows, inclusive (row 1 is the header)
LOOKBACK_DAYS = 90
MAX_TRIES = 4
HEADLESS = True
# Google may ask you to sign in before it lets you sort reviews. If that happens, point
# this at a Chrome profile folder you've signed into once (e.g. HERE / "chrome_profile")
CHROME_PROFILE = None

OUT_DIR = HERE / "review_data"
OUT_CSV = OUT_DIR / "reviews_master.csv"
LOG_CSV = OUT_DIR / "scrape_log.csv"

FIELDS = ["review_id", "place_ftid", "camis", "restaurant_dba", "gmaps_name", "needs_review",
          "reviewer_name", "rating", "review_date_relative", "review_date_est", "date_precision",
          "age_days_est", "has_text", "review_text", "scraped_at", "run_id"]
LOG_FIELDS = ["run_id", "scraped_at", "place_ftid", "restaurant_dba", "sheet_rows",
              "status", "cards_loaded", "in_window", "new"]

CARD = "div[data-review-id][aria-label]"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# the review list lives in the biggest scrollable div on the left side
PANE_JS = """
let best = null;
for (const n of document.querySelectorAll('div')) {
  const ov = getComputedStyle(n).overflowY;
  if ((ov === 'auto' || ov === 'scroll') && n.scrollHeight > n.clientHeight + 100
      && n.getBoundingClientRect().left < 500 && (!best || n.scrollHeight > best.scrollHeight)) best = n;
}
return best;
"""
LAST_DATE_JS = """
const s = document.querySelectorAll('div[data-review-id][aria-label] span.rsqaWe');
return s.length ? s[s.length - 1].textContent : null;
"""

# Google only shows relative dates like "3 weeks ago"
UNIT_DAYS = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30.44, "year": 365.25}
AGO_RE = re.compile(r"\b(a|an|one|\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.I)


class SignInWall(Exception):
    pass


def parse_age(text):
    m = AGO_RE.search(text or "")
    if not m:
        return None, None
    n = 1 if m.group(1).lower() in ("a", "an", "one") else int(m.group(1))
    unit = m.group(2).lower()
    return n * UNIT_DAYS[unit], unit


def load_places():
    # several CAMIS records can point to the same Google listing, so group by FTID
    ws = openpyxl.load_workbook(PLACES_XLSX, read_only=True).active
    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    places = {}
    for row_num, row in enumerate(rows, start=2):
        if not FIRST_ROW <= row_num <= LAST_ROW:
            continue
        r = dict(zip(header, row))
        ftid = (r["FTID"] or "").strip()
        if not ftid:
            print(f"row {row_num}: no FTID for {r['DBA']}, skipping")
            continue
        p = places.setdefault(ftid, {
            "ftid": ftid,
            "url": r["PLACE_URL"] or f"https://www.google.com/maps?ftid={ftid}&hl=en",
            "gmaps_name": r["GMAPS_NAME"],
            "camis": [], "dba": [], "rows": [], "needs_review": False,
        })
        p["camis"].append(str(r["CAMIS"]))
        p["dba"].append(str(r["DBA"]))
        p["rows"].append(str(row_num))
        p["needs_review"] |= str(r["NEEDS_REVIEW"]).lower() == "true"
    return list(places.values())


def start_browser():
    opts = webdriver.ChromeOptions()
    if HEADLESS:
        opts.add_argument("--headless=new")
    if CHROME_PROFILE:
        opts.add_argument(f"--user-data-dir={CHROME_PROFILE}")
    opts.add_argument("--window-size=1200,1400")
    opts.add_argument("--lang=en-US")
    opts.add_argument(f"--user-agent={UA}")
    opts.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
    d = webdriver.Chrome(options=opts)
    # a fresh profile gets a stripped-down Maps page with no Reviews tab on its
    # first load, so visit Maps once to get the cookies set
    d.get("https://www.google.com/maps?hl=en")
    time.sleep(4)
    return d


def js_click(d, el):
    d.execute_script("arguments[0].click();", el)


def close_popups(d):
    for b in d.find_elements(By.XPATH, '//button[normalize-space()="Dismiss" or @aria-label="Dismiss"]'):
        try:
            if b.is_displayed():
                js_click(d, b)
        except WebDriverException:
            pass


def signin_wall(d):
    xpath = '//*[contains(text(), "Sign-in to get the best of Google Maps")]'
    return any(e.is_displayed() for e in d.find_elements(By.XPATH, xpath))


def find_reviews_tab(d):
    for b in d.find_elements(By.CSS_SELECTOR, 'button[role="tab"]'):
        if (b.get_attribute("aria-label") or "").startswith("Reviews for") and b.is_displayed():
            return b
    return None


def find_newest(d):
    for item in d.find_elements(By.CSS_SELECTOR, '[role="menuitemradio"]'):
        if item.is_displayed() and item.text.strip() == "Newest":
            return item
    return None


def wait(d, seconds):
    # Maps re-renders a lot, so elements found a moment ago often go stale
    return WebDriverWait(d, seconds, poll_frequency=0.25, ignored_exceptions=[StaleElementReferenceException])


def open_reviews_newest(d, url):
    d.get(url)
    tab = wait(d, 20).until(find_reviews_tab)
    close_popups(d)
    js_click(d, tab)
    time.sleep(1.5)

    # the Sort button shows up before it actually works, so keep clicking until the menu opens
    sort_css = 'button[aria-label="Sort reviews"], button[data-value="Sort"]'
    for i in range(12):
        close_popups(d)
        try:
            btn = wait(d, 15).until(lambda d: d.find_elements(By.CSS_SELECTOR, sort_css))[0]
            d.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            time.sleep(0.5)
            if i % 2 == 0:
                btn.click()
            else:
                js_click(d, btn)
            newest = wait(d, 3).until(find_newest)
            js_click(d, newest)
            time.sleep(2)
            return
        except TimeoutException:
            if signin_wall(d):
                raise SignInWall()
        except WebDriverException:   # button got re-rendered or covered, try again
            time.sleep(1)
    raise RuntimeError("could not sort by Newest")


def count_cards(d):
    return len(d.find_elements(By.CSS_SELECTOR, CARD))


def scroll_reviews(d):
    pane = d.execute_script(PANE_JS)
    if pane is None:   # only a few reviews, everything is already on the page
        return "short_list"
    count, stalls = count_cards(d), 0
    for i in range(300):
        if i % 10 == 0:
            close_popups(d)
        d.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", pane)
        # move on as soon as new cards show up instead of sleeping a fixed time
        try:
            wait(d, 3).until(lambda d: count_cards(d) > count)
            stalls = 0
        except TimeoutException:
            stalls += 1
            if stalls >= 3:
                return "end_of_list"
        count = count_cards(d)
        age, _ = parse_age(d.execute_script(LAST_DATE_JS))
        if age is not None and age > LOOKBACK_DAYS:
            return "reached_lookback"
    return "max_scrolls"


def expand_text(d):
    for btn in d.find_elements(By.CSS_SELECTOR, 'button.w8nwRe, button[aria-label="See more"]'):
        try:
            js_click(d, btn)
        except WebDriverException:
            pass
    time.sleep(1)


def parse_reviews(html, place, scraped_at, run_id):
    soup = BeautifulSoup(html, "lxml")
    reviews, seen = [], set()
    for card in soup.select(CARD):
        rid = card["data-review-id"]
        if rid in seen:   # nested elements can repeat the same review
            continue
        seen.add(rid)
        name = card.find("div", class_="d4r55")
        rating = card.find("span", class_="kvMYJc")
        date = card.find("span", class_="rsqaWe")
        text = card.find("span", class_="wiI7pd")
        date_text = date.get_text(strip=True) if date else None
        age, unit = parse_age(date_text)
        reviews.append({
            "review_id": rid,
            "place_ftid": place["ftid"],
            "camis": "; ".join(place["camis"]),
            "restaurant_dba": "; ".join(place["dba"]),
            "gmaps_name": place["gmaps_name"],
            "needs_review": place["needs_review"],
            "reviewer_name": name.get_text(strip=True) if name else card.get("aria-label"),
            "rating": rating.get("aria-label") if rating else None,
            "review_date_relative": date_text,
            "review_date_est": (scraped_at - timedelta(days=age)).date().isoformat() if age is not None else None,
            "date_precision": unit,
            "age_days_est": round(age, 1) if age is not None else None,
            "has_text": bool(text),
            "review_text": text.get_text(" ", strip=True) if text else None,
            "scraped_at": scraped_at.isoformat(),
            "run_id": run_id,
        })
    return reviews


def scrape_place(driver, place):
    """Returns (driver, html or None, status). The driver may be replaced if it breaks."""
    for attempt in range(1, MAX_TRIES + 1):
        try:
            if driver is None:
                driver = start_browser()
            open_reviews_newest(driver, place["url"])
            status = scroll_reviews(driver)
            expand_text(driver)
            return driver, driver.page_source, status
        except SignInWall:
            # some browser sessions always ask for sign-in when sorting; a new one usually doesn't
            print(f"  try {attempt}: sign-in wall, restarting browser")
            driver = quit_browser(driver)
        except Exception as e:
            print(f"  try {attempt}: {type(e).__name__}")
            try:
                driver.title
            except Exception:
                driver = quit_browser(driver)
    return driver, None, "failed"


def quit_browser(driver):
    try:
        driver.quit()
    except Exception:
        pass
    return None


def write_csv(path, fields, rows, append=False):
    new_file = not path.exists() or not append
    with open(path, "a" if append else "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)


def main():
    OUT_DIR.mkdir(exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    places = load_places()
    print(f"run {run_id}: {len(places)} places from rows {FIRST_ROW}-{LAST_ROW}")

    master = {}
    if OUT_CSV.exists():
        with open(OUT_CSV, newline="", encoding="utf-8") as f:
            master = {r["review_id"]: r for r in csv.DictReader(f)}

    driver, failed = None, []
    try:
        for i, place in enumerate(places, 1):
            label = f"[{i}/{len(places)}] {place['dba'][0]} (row {', '.join(place['rows'])})"
            print(label)
            scraped_at = datetime.now(timezone.utc).replace(microsecond=0)
            driver, html, status = scrape_place(driver, place)
            reviews = parse_reviews(html, place, scraped_at, run_id) if html else []
            if not html:
                failed.append(label)

            ages = [r["age_days_est"] for r in reviews if r["age_days_est"] is not None]
            if any(b < a for a, b in zip(ages, ages[1:])):
                print("  warning: reviews not in date order, Newest sort may not have applied")

            recent = [r for r in reviews if r["age_days_est"] is not None and r["age_days_est"] <= LOOKBACK_DAYS]
            new = [r for r in recent if r["review_id"] not in master]
            # keep the first copy of a review, its relative date was the most precise
            for r in new:
                master[r["review_id"]] = r
            if new:
                rows = sorted(master.values(), key=lambda r: (r["place_ftid"], r["review_date_est"] or ""), reverse=True)
                write_csv(OUT_CSV, FIELDS, rows)

            write_csv(LOG_CSV, LOG_FIELDS, [{
                "run_id": run_id, "scraped_at": scraped_at.isoformat(), "place_ftid": place["ftid"],
                "restaurant_dba": "; ".join(place["dba"]), "sheet_rows": "; ".join(place["rows"]),
                "status": status, "cards_loaded": len(reviews), "in_window": len(recent), "new": len(new),
            }], append=True)
            print(f"  {status}: {len(reviews)} loaded, {len(recent)} in last {LOOKBACK_DAYS} days, {len(new)} new")
            time.sleep(2)
    finally:
        quit_browser(driver)

    print(f"\ndone: {len(places) - len(failed)} ok, {len(failed)} failed, {len(master)} reviews in {OUT_CSV.name}")
    for label in failed:
        print("  failed:", label)


if __name__ == "__main__":
    main()
