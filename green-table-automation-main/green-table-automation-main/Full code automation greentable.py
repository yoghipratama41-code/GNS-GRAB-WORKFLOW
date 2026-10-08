import os
import re
import json
import time
import random
from datetime import datetime, timedelta

import easyocr
import gspread
import pandas as pd
import streamlit as st
from PIL import Image, ImageEnhance

from google.oauth2.service_account import Credentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.auth.transport.requests import Request as GoogleAuthRequest
import google.generativeai as genai

# Additional libraries for Google Slides & Drive API
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ==========================================
# 0. PAGE CONFIGURATION
# ==========================================
st.set_page_config(page_title="GNS Greentable & Gems Automator", page_icon="ðŸŸ¢", layout="wide")

SPREADSHEET_ID = "1FvJBZvtjKQ3QiZEHCOic2lZtegbuRK0wmsE6jCGTooc"
TAB_MINGGUAN = "Bonus Mingguan_This week"
TAB_PUNCAK = "Bonus Tambang Puncak_This Week"

# Gems Automator Configuration
GEMS_SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1W6t8fCOPB_dFlWnzHSpVJ66tAnOcHHnMn1jXtHfC2S4/edit?usp=sharing"
GEMS_SLIDES_ID = "175o7VUG_h7IFAlgiin-2clS1dt-9O4WwD4XnJTozHgc"  # First target Slide ID
GEMS_SLIDES_ID_2 = "1PmJhzr95FXirUM2YihGey7fgQBsNnczNxZwxkKFwXN8" # Second target Slide ID (new)

TAHUN_SEKARANG = datetime.now().year

MODEL_PRIORITY = [
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-1.5-flash",
]

# Extra scopes to support Google Slides and Google Drive (for placeholder image upload handling)
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/drive"
]

# ==========================================
# OAuth (regular user account) only for Slides & Drive
# ==========================================
OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/drive.file "
    "https://www.googleapis.com/auth/drive.readonly "
    "https://www.googleapis.com/auth/presentations"
)
OAUTH_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# ==========================================
# 1. GOOGLE SERVICES CONNECTION
# ==========================================
@st.cache_resource
def get_google_credentials():
    return Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=SCOPES
    )

@st.cache_resource(ttl=1800)  # 30-minute cache so the token isn't refreshed on every rerun/interaction
def get_slides_drive_oauth_creds():
    creds = OAuthCredentials(
        token=None,
        refresh_token=st.secrets["GOOGLE_REFRESH_TOKEN"],
        token_uri=OAUTH_TOKEN_ENDPOINT,
        client_id=st.secrets["GOOGLE_CLIENT_ID"],
        client_secret=st.secrets["GOOGLE_CLIENT_SECRET"],
        scopes=OAUTH_SCOPES.split(),
    )
    creds.refresh(GoogleAuthRequest())
    return creds

@st.cache_resource
def get_spreadsheet():
    creds = get_google_credentials()
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)

def upload_to_gsheets(df, tab_name):
    sh = get_spreadsheet()
    try:
        worksheet = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        st.error(f"âŒ Tab '{tab_name}' not found in the spreadsheet. Upload cancelled.")
        return False

    data_to_upload = [df.columns.values.tolist()] + df.fillna("").values.tolist()
    worksheet.clear()
    worksheet.update(data_to_upload, value_input_option="USER_ENTERED")
    return True

@st.cache_resource
def get_gems_sheet():
    creds = get_google_credentials()
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(GEMS_SPREADSHEET_URL)
    return sh.worksheet("This week")

# ==========================================
# 2. OCR ENGINE CACHE & GEMINI SETUP
# ==========================================
@st.cache_resource(show_spinner="Loading OCR Engine... (please wait a moment)")
def load_ocr_engine():
    return easyocr.Reader(["en"], gpu=False)

def get_model_fallback_list(api_key):
    genai.configure(api_key=api_key)
    available = [m.name for m in genai.list_models() if "generateContent" in m.supported_generation_methods]
    ordered = []
    for key in MODEL_PRIORITY:
        match = next((m for m in available if key in m), None)
        if match and match not in ordered:
            ordered.append(match)
    if not ordered:
        ordered = [m for m in available if "flash" in m]
    return ordered

# ==========================================
# 3. MODULE: WEEKLY & DAILY BONUS
# ==========================================
def run_ocr(reader, image_path):
    results = reader.readtext(image_path)
    def sort_key(item):
        bbox, text, conf = item
        y = min(p[1] for p in bbox)
        x = min(p[0] for p in bbox)
        return (round(y / 10), x)
    return sorted(results, key=sort_key)

def crop_to_part1(ocr_results):
    start_y, end_y = None, None
    for bbox, text, conf in ocr_results:
        y = min(p[1] for p in bbox)
        t = text.lower()
        if "bonus mingguan" in t and start_y is None:
            start_y = y
        if "pick-up" in t or "jarak jauh" in t:
            end_y = y
            break
    if start_y is None:
        raise ValueError("Section 'Bonus Mingguan' not found in OCR results.")
    if end_y is None:
        end_y = float("inf")
    return [r for r in ocr_results if start_y - 5 <= min(p[1] for p in r[0]) < end_y]

TEMPOH_RE = re.compile(r"Tempoh\s*(\d+)\s*[:.]?\s*(.+)", re.IGNORECASE)
MONEY_RE = re.compile(r"RM\s?\d+", re.IGNORECASE)
NUM_RE = re.compile(r"^\d{1,4}$")

def group_rows_by_y(items, y_tol=12):
    rows = []
    for bbox, text, conf in items:
        y = min(p[1] for p in bbox)
        x = min(p[0] for p in bbox)
        placed = False
        for row in rows:
            if abs(row["y"] - y) <= y_tol:
                row["items"].append((x, text))
                placed = True
                break
        if not placed:
            rows.append({"y": y, "items": [(x, text)]})
    for row in rows:
        row["items"].sort(key=lambda t: t[0])
    rows.sort(key=lambda r: r["y"])
    return rows

def parse_mingguan_section(part1_items):
    rows = group_rows_by_y(part1_items)
    periods = {}
    current_period = None
    for row in rows:
        line_text = " ".join(t for _, t in row["items"])
        m = TEMPOH_RE.search(line_text)
        if m and "jul" in line_text.lower() or (m and "jun" in line_text.lower()):
            label = f"Tempoh {m.group(1)}"
            current_period = label
            periods[current_period] = {"date_range": m.group(2).strip(), "tiers": []}
            continue
        if "bonus harian" in line_text.lower():
            break
        if current_period and any(c.isdigit() for c in line_text):
            nums = [t for _, t in row["items"] if NUM_RE.match(t)]
            monies = [t for _, t in row["items"] if MONEY_RE.search(t)]
            if len(nums) == 1 and len(monies) == 2:
                periods[current_period]["tiers"].append({
                    "jumlah_pesanan": int(nums[0]),
                    "biasa": monies[0].replace(" ", ""),
                    "berganda": monies[1].replace(" ", ""),
                })
    return periods

def parse_harian_section(part1_items):
    rows = group_rows_by_y(part1_items)
    date_range, tier, in_harian = None, None, False
    for row in rows:
        line_text = " ".join(t for _, t in row["items"])
        if "bonus harian" in line_text.lower():
            in_harian = True
            continue
        if in_harian:
            m = TEMPOH_RE.search(line_text)
            if m:
                date_range = m.group(2).strip()
                continue
            nums = [t for _, t in row["items"] if NUM_RE.match(t)]
            monies = [t for _, t in row["items"] if MONEY_RE.search(t)]
            if len(nums) == 1 and len(monies) == 1:
                tier = {"jumlah_pesanan": int(nums[0]), "bonus": monies[0].replace(" ", "")}
                break
    return {"date_range": date_range, "tier": tier}

GEMINI_BM_PASS_1 = """
You are looking at an image of a rider incentive table (Indonesian/Malay).
Extract ONLY the "Bonus Mingguan" and "Bonus Harian" sections (do not take Tambang Puncak / other sections).
Return JSON in EXACTLY this format, with no extra text:
{
  "bonus_mingguan": [
    {
      "tempoh_label": "Tempoh 1",
      "date_range": "29 Jun - 2 Jul 2026",
      "tiers": [
        {"jumlah_pesanan": 30, "biasa": "RM20", "berganda": "RM35"}
      ]
    }
  ],
  "bonus_harian": {
    "date_range": "29 Jun - 5 Jul 2026",
    "jumlah_pesanan": 15,
    "bonus": "RM10"
  }
}
"""
GEMINI_BM_PASS_2 = """
This is the same table image, and your earlier JSON result:
{JSON_SEBELUMNYA}
Task (Checking): Re-check the image very carefully.
Is any "Tempoh" (period) missing? Make sure every Tempoh has exactly 5 tiers.
If any tier or daily bonus is missing, add it. Ensure number accuracy & a perfectly valid JSON format.
Return ONLY the final revised JSON format, without markdown.
"""

def _call_with_retry(model, contents, nama_model_pendek, step_label, log, max_retry=2):
    delay = 5
    last_err = None
    for attempt in range(max_retry):
        try:
            resp = model.generate_content(contents)
            return re.sub(r"^```json|```$", "", resp.text.strip(), flags=re.MULTILINE).strip()
        except Exception as e:
            err_msg = str(e)
            last_err = e
            if "429" in err_msg or "503" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                log(f"â³ [{nama_model_pendek}] {step_label}: rate limited, retry in {delay:.0f}s...")
                time.sleep(delay + random.uniform(0, 2))
                delay *= 2
            else:
                raise
    raise last_err

def validate_mingguan_json(result):
    """Ensure the JSON structure has the right keys before approval."""
    if not isinstance(result, dict):
        raise ValueError("JSON output is not a dictionary.")
    
    bm = result.get("bonus_mingguan", [])
    if not isinstance(bm, list):
        raise ValueError("Key 'bonus_mingguan' must be a list.")
    
    for period in bm:
        if "tiers" not in period:
            raise ValueError("Every period in 'bonus_mingguan' must have a 'tiers' key.")
        for tier in period["tiers"]:
            if not all(k in tier for k in ("jumlah_pesanan", "biasa", "berganda")):
                raise ValueError("Some tiers in 'bonus_mingguan' are missing keys (jumlah_pesanan/biasa/berganda).")

    bh = result.get("bonus_harian", {})
    if bh:
        if not all(k in bh for k in ("date_range", "jumlah_pesanan", "bonus")):
            raise ValueError("Key 'bonus_harian' is missing fields (date_range/jumlah_pesanan/bonus).")

def run_gemini_mingguan_fallback(image_path, api_key, log=st.write):
    model_fallback_list = get_model_fallback_list(api_key)
    img = Image.open(image_path)
    last_err = None
    for model_name in model_fallback_list:
        model = genai.GenerativeModel(model_name)
        nama_model_pendek = model_name.split("/")[-1]
        try:
            log(f"â³ [{nama_model_pendek}] Pass 1/2: Generate...")
            json_1 = _call_with_retry(model, [GEMINI_BM_PASS_1, img], nama_model_pendek, "Generate", log)
            log(f"ðŸ” [{nama_model_pendek}] Pass 2/2: Checking...")
            json_2 = _call_with_retry(
                model, [GEMINI_BM_PASS_2.replace("{JSON_SEBELUMNYA}", json_1), img],
                nama_model_pendek, "Checking", log,
            )
            result = json.loads(json_2)
            
            # --- VALIDASI STRUKTUR JSON ---
            validate_mingguan_json(result)
            
            log(f"âœ… Succeeded with model **{nama_model_pendek}**.")
            return result
        except Exception as e:
            last_err = e
            log(f"âš ï¸ [{nama_model_pendek}] failed validation/extraction ({e}), trying next model...")
            continue
    raise Exception(f"Weekly fallback failed on all models. Last error: {last_err}")

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6,
    "jul": 7, "ogo": 8, "aug": 8, "sep": 9, "okt": 10, "oct": 10,
    "nov": 11, "dis": 12, "dec": 12,
}

def parse_date_range(date_range_str):
    m = re.search(r"(\d{1,2})\s*([A-Za-z]{3,})?\s*-\s*(\d{1,2})\s*([A-Za-z]{3,})\s*(\d{4})", date_range_str.strip())
    if not m:
        raise ValueError(f"Could not parse date: {date_range_str}")
    start_day, start_month_str, end_day, end_month_str, year = m.groups()
    end_month = MONTH_MAP[end_month_str.strip().lower()[:3]]
    start_month = MONTH_MAP[start_month_str.strip().lower()[:3]] if start_month_str else end_month
    start_date = datetime(int(year), start_month, int(start_day)).date()
    end_date = datetime(int(year), end_month, int(end_day)).date()
    dates, d = [], start_date
    while d <= end_date:
        dates.append(d)
        d += timedelta(days=1)
    return dates

def to_number(val):
    if not val:
        return ""
    num_str = re.sub(r"[^\d]", "", str(val))
    return int(num_str) if num_str else ""

def build_sheet_rows_mingguan(bonus_mingguan, bonus_harian):
    rows = []
    harian_dates = parse_date_range(bonus_harian["date_range"]) if bonus_harian else []
    harian_lookup = set(harian_dates)
    all_dates = set(harian_dates)
    period_for_date = {}
    for period in bonus_mingguan:
        p_dates = parse_date_range(period["date_range"])
        all_dates |= set(p_dates)
        for d in p_dates:
            period_for_date[d] = period
    for d in sorted(all_dates):
        if d in harian_lookup:
            rows.append({
                "Date": d.strftime("%Y-%m-%d"),
                "Total Order": to_number(bonus_harian["jumlah_pesanan"]),
                "Regular Bonus Rate": to_number(bonus_harian["bonus"]),
                "Double Bonus Rate": "",
            })
        period = period_for_date.get(d)
        if period:
            for tier in period["tiers"]:
                rows.append({
                    "Date": d.strftime("%Y-%m-%d"),
                    "Total Order": to_number(tier["jumlah_pesanan"]),
                    "Regular Bonus Rate": to_number(tier["biasa"]),
                    "Double Bonus Rate": to_number(tier["berganda"]),
                })
    return pd.DataFrame(rows)

def validate_mingguan_extraction(bonus_mingguan, bonus_harian):
    if not bonus_mingguan:
        return False
    for p in bonus_mingguan:
        if len(p.get("tiers", [])) != 5:
            return False
    if not bonus_harian or not bonus_harian.get("tier"):
        return False
    return True

def extract_mingguan(reader, image_path, api_key, log=st.write):
    log("ðŸ”Ž [WEEKLY MODULE] Trying OCR (EasyOCR)...")
    try:
        ocr_results = run_ocr(reader, image_path)
        part1_items = crop_to_part1(ocr_results)
        mingguan_raw = parse_mingguan_section(part1_items)
        harian_raw = parse_harian_section(part1_items)
        bonus_mingguan = [
            {"tempoh_label": k, "date_range": v["date_range"], "tiers": v["tiers"]}
            for k, v in mingguan_raw.items()
        ]
        bonus_harian = {
            "date_range": harian_raw["date_range"],
            "jumlah_pesanan": harian_raw["tier"]["jumlah_pesanan"],
            "bonus": harian_raw["tier"]["bonus"],
        } if harian_raw["tier"] else None
        if validate_mingguan_extraction(bonus_mingguan, {"tier": bonus_harian} if bonus_harian else None):
            log("âœ… OCR completed successfully.")
            return bonus_mingguan, bonus_harian
        else:
            log("âš ï¸ OCR incomplete, switching to Gemini...")
    except Exception as e:
        log(f"âš ï¸ OCR failed ({e}), switching to Gemini...")
    if not api_key:
        raise ValueError("OCR failed and the Gemini API Key has not been entered in the sidebar.")
    result = run_gemini_mingguan_fallback(image_path, api_key, log=log)
    return result.get("bonus_mingguan", []), result.get("bonus_harian", {})

# ==========================================
# 4. MODULE: TAMBANG PUNCAK
# ==========================================
def get_peak_prompts(year):
    pass_1 = f"""
You are a data analyst. Look at this rider incentive table image.
Your task is to focus ONLY on the "Tambang Puncak" (Peak Fare) section.
Convert that matrix data into a list of objects (unpivoted) using these rules:
1. ONLY extract data for the FIRST 7 DAYS (Monday through Sunday). If there is a following Monday column, IGNORE IT.
2. ONLY take hour cells that HAVE content (e.g. 15%, 20%). If a cell is empty, do NOT include it.
3. Compute 'Date' (YYYY-MM-DD) from the date in the column header. Assume the year is {year}.
4. Set 'Day' in English (Monday, Tuesday, etc.).
5. Write 'Hour' explicitly in AM/PM format (e.g. "12 AM", "1 PM").
6. 'Bonus' is a percentage string.
Return JSON in EXACTLY this format, with no extra text or markdown:
[
  {{"Date": "{year}-06-29", "Day": "Monday", "Hour": "12 AM", "Bonus": "15%"}}
]
"""
    pass_2 = """
This is your earlier JSON:
{JSON_SEBELUMNYA}
Task (Checking):
Some cells are often MISSED, e.g. Friday at 5 PM (17:00) or at shift-change hours.
RE-SCAN the image carefully. If any box with a percentage is not yet in the JSON, ADD IT.
Conversely, make sure there are no "ghost hours" (empty cells in the image but present in the JSON).
Return ONLY the final revised JSON format, without markdown.
"""
    return pass_1, pass_2

def validate_puncak_json(result):
    """Ensure the JSON structure has the right keys before approval."""
    if not isinstance(result, list):
        raise ValueError("Tambang Puncak JSON output must be a list array.")
    for item in result:
        if not isinstance(item, dict):
             raise ValueError("Tambang Puncak list entries must be dictionaries/objects.")
        if not all(k in item for k in ("Date", "Day", "Hour", "Bonus")):
            raise ValueError("Some Tambang Puncak item keys are missing (Date/Day/Hour/Bonus).")

def extract_peak_fare(image_path, api_key, year=TAHUN_SEKARANG, log=st.write):
    model_fallback_list = get_model_fallback_list(api_key)
    img = Image.open(image_path)
    pass_1, pass_2 = get_peak_prompts(year)
    last_err = None
    log(f"ðŸ”Ž [TAMBANG PUNCAK MODULE] Starting extraction (Year: {year})")
    for model_name in model_fallback_list:
        model = genai.GenerativeModel(model_name)
        nama_model_pendek = model_name.split("/")[-1]
        try:
            log(f"â³ [{nama_model_pendek}] Pass 1/2: Generate...")
            json_1 = _call_with_retry(model, [pass_1, img], nama_model_pendek, "Generate", log)
            log(f"ðŸ” [{nama_model_pendek}] Pass 2/2: Checking...")
            json_2 = _call_with_retry(
                model, [pass_2.replace("{JSON_SEBELUMNYA}", json_1), img],
                nama_model_pendek, "Checking", log,
            )
            result = json.loads(json_2)
            
            # --- VALIDASI STRUKTUR JSON ---
            validate_puncak_json(result)
            
            log(f"âœ… Succeeded with model **{nama_model_pendek}**.")
            return result
        except Exception as e:
            last_err = e
            log(f"âš ï¸ [{nama_model_pendek}] failed validation/extraction ({e}), trying next model...")
            continue
    raise Exception(f"Peak extraction failed on all models. Last error: {last_err}")

def to_percentage(val):
    if not val:
        return ""
    num_str = re.sub(r"[^\d.]", "", str(val))
    if num_str:
        return float(num_str) / 100.0
    return ""

def process_raw_data_puncak(raw_data):
    df = pd.DataFrame(raw_data)
    df["Bonus"] = df["Bonus"].apply(to_percentage)
    df["Hour"] = df["Hour"].astype(str).str.replace(r"^0\s*AM$", "12 AM", regex=True, flags=re.IGNORECASE)
    df["Hour"] = pd.to_datetime(df["Hour"], format="%I %p").dt.hour
    df["Date_Obj"] = pd.to_datetime(df["Date"])
    min_date = df["Date_Obj"].min()
    df = df[df["Date_Obj"] < min_date + pd.Timedelta(days=7)]
    df = df.sort_values(by=["Date_Obj", "Hour"]).drop(columns=["Date_Obj"])
    return df.reset_index(drop=True)


# ==========================================
# 4B. NEW MODULE: COMPARE THIS WEEK VS LAST WEEK
# ==========================================
def compare_tempoh_tiers(tiers_this, tiers_last):
    lines = []
    any_change = False
    n = max(len(tiers_this), len(tiers_last))
    for i in range(n):
        t_this = tiers_this[i] if i < len(tiers_this) else None
        t_last = tiers_last[i] if i < len(tiers_last) else None

        if t_this is None:
            lines.append(f"{t_last['jumlah_pesanan']} orders: removed this week (was {t_last['biasa']}/{t_last['berganda']}).")
            any_change = True
            continue
        if t_last is None:
            lines.append(f"{t_this['jumlah_pesanan']} orders: new this week ({t_this['biasa']}/{t_this['berganda']}).")
            any_change = True
            continue

        label = "Entry tier" if i == 0 else f"{t_this['jumlah_pesanan']} orders"
        target_changed = t_this["jumlah_pesanan"] != t_last["jumlah_pesanan"]
        biasa_changed = t_this["biasa"] != t_last["biasa"]
        berganda_changed = t_this["berganda"] != t_last["berganda"]

        if not target_changed and not biasa_changed and not berganda_changed:
            lines.append(f"{label}: no change ({t_this['biasa']}/{t_this['berganda']}).")
            continue

        any_change = True
        segs = []
        if target_changed:
            d = "up" if t_this["jumlah_pesanan"] > t_last["jumlah_pesanan"] else "down"
            segs.append(f"target {d} from {t_last['jumlah_pesanan']} to {t_this['jumlah_pesanan']} orders")

        if biasa_changed and berganda_changed:
            old_b, new_b = to_number(t_last["biasa"]), to_number(t_this["biasa"])
            old_g, new_g = to_number(t_last["berganda"]), to_number(t_this["berganda"])
            same_dir = (
                old_b not in ("", None) and old_g not in ("", None)
                and (new_b - old_b) * (new_g - old_g) >= 0
            )
            if same_dir:
                d = "up" if (new_b + new_g) >= (old_b + old_g) else "down"
                segs.append(f"bonus {d} ({t_last['biasa']}/{t_last['berganda']} â†’ {t_this['biasa']}/{t_this['berganda']})")
            else:
                bd = "up" if new_b > old_b else "down"
                gd = "up" if new_g > old_g else "down"
                segs.append(f"Biasa {bd} ({t_last['biasa']} â†’ {t_this['biasa']})")
                segs.append(f"Berganda {gd} ({t_last['berganda']} â†’ {t_this['berganda']})")
        elif biasa_changed:
            d = "up" if to_number(t_this["biasa"]) > to_number(t_last["biasa"]) else "down"
            segs.append(f"Biasa {d} ({t_last['biasa']} â†’ {t_this['biasa']})")
        elif berganda_changed:
            d = "up" if to_number(t_this["berganda"]) > to_number(t_last["berganda"]) else "down"
            segs.append(f"Berganda {d} ({t_last['berganda']} â†’ {t_this['berganda']})")

        lines.append(f"{label}: " + ", ".join(segs) + ".")

    return lines, any_change


def compare_bonus_harian(harian_this, harian_last):
    if not harian_this or not harian_last:
        return True, "No Daily Bonus data available to compare in one of the images."

    same_target = harian_this["jumlah_pesanan"] == harian_last["jumlah_pesanan"]
    same_bonus = harian_this["bonus"] == harian_last["bonus"]
    if same_target and same_bonus:
        return False, f"No change â€” still {harian_this['bonus']} for {harian_this['jumlah_pesanan']} orders."

    segs = []
    if not same_target:
        d = "up" if harian_this["jumlah_pesanan"] > harian_last["jumlah_pesanan"] else "down"
        segs.append(f"target {d} from {harian_last['jumlah_pesanan']} to {harian_this['jumlah_pesanan']} orders")
    if not same_bonus:
        d = "up" if to_number(harian_this["bonus"]) > to_number(harian_last["bonus"]) else "down"
        segs.append(f"bonus {d} ({harian_last['bonus']} â†’ {harian_this['bonus']})")
    text = ", ".join(segs)
    return True, text[0].upper() + text[1:] + "."


def compare_peak_fare(df_this, df_last):
    merged = pd.merge(
        df_this[["Day", "Hour", "Bonus"]],
        df_last[["Day", "Hour", "Bonus"]],
        on=["Day", "Hour"], how="outer", suffixes=("_this", "_last"), indicator=True,
    )
    diffs = merged[(merged["_merge"] != "both") | (merged["Bonus_this"] != merged["Bonus_last"])]
    if diffs.empty:
        return False, ["No change across all hours (AM and PM), all days."]

    day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    diffs = diffs.copy()
    diffs["day_rank"] = diffs["Day"].apply(lambda d: day_order.index(d) if d in day_order else 99)
    diffs = diffs.sort_values(["day_rank", "Hour"])

    def hour_label(h):
        return pd.Timestamp(year=2000, month=1, day=1, hour=int(h)).strftime("%I %p").lstrip("0")

    lines = []
    for day in day_order:
        day_diffs = diffs[diffs["Day"] == day]
        if day_diffs.empty:
            continue
        entries = []
        for _, row in day_diffs.iterrows():
            hl = hour_label(row["Hour"])
            old_v, new_v = row.get("Bonus_last"), row.get("Bonus_this")
            if pd.isna(old_v):
                entries.append(f"{hl}: new ({new_v * 100:.0f}%)")
            elif pd.isna(new_v):
                entries.append(f"{hl}: removed (was {old_v * 100:.0f}%)")
            else:
                entries.append(f"{hl}: {old_v * 100:.0f}% â†’ {new_v * 100:.0f}%")
        lines.append(f"{day}: " + ", ".join(entries) + ".")
    return True, lines


def build_summary_lines(tempoh_results, harian_changed, puncak_changed):
    lines = []

    if not puncak_changed:
        lines.append("Peak Fare (Tambang Puncak) is fully unchanged this week.")
    else:
        lines.append("Peak Fare (Tambang Puncak) has some changes this week â€” see details below.")

    changed_labels = [t["label"] for t in tempoh_results if t["changed"]]
    unchanged_labels = [t["label"] for t in tempoh_results if not t["changed"]]
    if not changed_labels:
        lines.append("Weekly Bonus (Bonus Mingguan) is unchanged across all Tempoh this week.")
    else:
        parts = []
        if unchanged_labels:
            parts.append(f"{', '.join(unchanged_labels)} unchanged")
        parts.append(f"{', '.join(changed_labels)} has some tweaks â€” see details below")
        lines.append("Weekly Bonus (Bonus Mingguan): " + "; ".join(parts) + ".")

    if not harian_changed:
        lines.append("Everything else (Daily Bonus, Pick-up Distance Bonus, New Area Bonus, Ciri Slot Pilihan) stays the same.")
    else:
        lines.append("Daily Bonus has changes this week â€” see details below. Pick-up Distance Bonus, New Area Bonus, and Ciri Slot Pilihan stay the same.")

    return lines


def build_full_comparison_report(mingguan_this, harian_this, df_puncak_this,
                                   mingguan_last, harian_last, df_puncak_last):
    last_by_label = {p["tempoh_label"]: p for p in mingguan_last}
    tempoh_results = []
    mingguan_detail_lines = []

    for period in mingguan_this:
        label = period["tempoh_label"]
        date_range = period["date_range"]
        last_period = last_by_label.get(label)

        if last_period is None:
            tempoh_results.append({"label": label, "changed": True})
            mingguan_detail_lines.append(f"   * {label} ({date_range}): New period this week â€” no prior data to compare.")
            continue

        tier_lines, changed = compare_tempoh_tiers(period["tiers"], last_period["tiers"])
        tempoh_results.append({"label": label, "changed": changed})

        if not changed:
            mingguan_detail_lines.append(f"   * {label} ({date_range}): No change â€” same targets and payouts as last week's {label}.")
        else:
            mingguan_detail_lines.append(f"   * {label} ({date_range}) vs last week's {label}:")
            for tl in tier_lines:
                mingguan_detail_lines.append(f"      * {tl}")

    harian_changed, harian_text = compare_bonus_harian(harian_this, harian_last)
    puncak_changed, puncak_lines = compare_peak_fare(df_puncak_this, df_puncak_last)
    summary_lines = build_summary_lines(tempoh_results, harian_changed, puncak_changed)

    out = ["* Summary"]
    out += [f"   * {s}" for s in summary_lines]

    out.append("* Weekly Bonus / Bonus Mingguan")
    out += mingguan_detail_lines

    out.append("* Daily Bonus / Bonus Harian")
    out.append(f"   * {harian_text}")

    out.append("* Peak Fare / Tambang Puncak")
    out += [f"   * {pl}" for pl in puncak_lines]

    out.append("* Pick-up Distance Bonus, New Area Bonus, Ciri Slot Pilihan")
    out.append("   * No change.")

    out.append("")
    out.append("Thank you everyone, and we'll keep you posted on any further updates!")

    return "\n".join(out)


# ==========================================
# 5. MODULE: GEMS AUTOMATOR & SLIDES INTEGRATION
# ==========================================
def extract_gems_rewards(ocr_result_list):
    start_idx, end_idx = -1, len(ocr_result_list)
    for idx, text in enumerate(ocr_result_list):
        if "target" in text.lower():
            start_idx = idx
        if "qualification" in text.lower() or "criteria" in text.lower():
            end_idx = idx
            break
    target_section_text = ocr_result_list[start_idx + 1: end_idx]
    gems_found, rewards_found = [], []
    for text in target_section_text:
        if "%" in text:
            continue
        matches = re.findall(r"\d+(?:\.\d+)?", text)
        for m in matches:
            val = float(m)
            if val <= 0 or val > 200:
                continue
            if "." in text or "$" in text or "s$" in text.lower():
                rewards_found.append(val)
            elif val.is_integer() and val < 150:
                gems_found.append(int(val))
    return sorted(list(set(gems_found))), sorted(list(set(rewards_found))), target_section_text

def analisis_dengan_ultimate_retry(model, prompt, gambar_list, max_retry=5):
    delay = 10
    for i in range(max_retry):
        try:
            time.sleep(2)
            respon = model.generate_content([prompt] + gambar_list)
            return respon.text
        except Exception as e:
            err_msg = str(e)
            if "429" in err_msg or "503" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                wait_time = delay + random.uniform(0, 5)
                st.warning(f"âš ï¸ Server busy. Waiting {wait_time:.1f} seconds... (attempt {i+1}/{max_retry})")
                time.sleep(wait_time)
                delay *= 2
            else:
                raise e
    raise Exception("Failed completely after maximum attempts.")

def extract_via_gemini_gems(image_path, api_key):
    if not api_key:
        st.error("âš ï¸ Gemini API Key has not been entered in the sidebar.")
        return [], [], None
    
    prompt = """Look at this screenshot of a delivery rider mission app.
Find the date of the mission (e.g. 15 Aug) and the "TARGET & REWARD" section. It contains exactly 3 tiers, each with:
- a "gems" target number (a small whole number)
- a corresponding "S$" reward amount
Assume the year is 2026 if not stated.
Return ONLY a raw JSON object: {"date": "DD MMM YYYY", "gems": [g1, g2, g3], "rewards": [r1, r2, r3]}"""
    
    try:
        img = Image.open(image_path)
        model_fallback_list = get_model_fallback_list(api_key)
        
        for model_name in model_fallback_list:
            gemini_model = genai.GenerativeModel(model_name)
            nama_model_pendek = model_name.split("/")[-1]
            try:
                teks_raw = analisis_dengan_ultimate_retry(gemini_model, prompt, [img], max_retry=2)
                clean_text = re.sub(r"^```json\s*|\s*```$", "", teks_raw.strip())
                data = json.loads(clean_text)
                
                gems = sorted([int(g) for g in data.get("gems", [])])
                rewards = sorted([float(r) for r in data.get("rewards", [])])
                ai_date = data.get("date", None)
                
                if len(gems) == 3 and len(rewards) == 3:
                    st.write(f"âœ… AI extracted successfully with model **{nama_model_pendek}**.")
                    return gems, rewards, ai_date
                else:
                    st.warning(f"âš ï¸ [{nama_model_pendek}] JSON format incomplete, trying another model...")
                    continue
            except Exception as e:
                st.warning(f"âš ï¸ [{nama_model_pendek}] failed ({e}), trying next model...")
                continue
                
        return [], [], None
    except Exception as e:
        st.error(f"âš ï¸ Error while processing AI: {e}")
        return [], [], None

def get_target_cells(tier, vehicle):
    tier_l = tier.lower()
    vehicle_l = vehicle.lower()
    if vehicle_l in ["e-bike", "bicycle"]:
        same_tier_group = ["E-bike", "Bicycle"]
    else:
        same_tier_group = ["Walker"] if vehicle_l == "walker" else [vehicle.capitalize()]
    targets = [(tier, v) for v in same_tier_group]
    if tier_l in ["diamond", "sapphire"] and vehicle_l != "walker":
        other_tier = "Sapphire" if tier_l == "diamond" else "Diamond"
        for v in same_tier_group:
            targets.append((other_tier, v))
    seen = set()
    unique_targets = []
    for t in targets:
        key = (t[0].lower(), t[1].lower())
        if key not in seen:
            seen.add(key)
            unique_targets.append(t)
    return unique_targets

# --- BUILD DATE STRINGS TO MATCH SLIDE TEMPLATES 1-2 ---
def get_slide_date_replacements(any_date):
    monday = any_date - timedelta(days=any_date.weekday())
    tuesday = monday + timedelta(days=1)
    wednesday = monday + timedelta(days=2)
    thursday = monday + timedelta(days=3)
    friday = monday + timedelta(days=4)
    saturday = monday + timedelta(days=5)
    sunday = monday + timedelta(days=6)
    
    months = ["", "July", "August", "September", "October", "November", "December", 
              "January", "February", "March", "April", "May", "June"]
    
    def fmt_full(d):
        return f"{d.day} {d.strftime('%B')}"

    date_monday_str = fmt_full(monday)
    date_wednesday_str = fmt_full(wednesday)

    if thursday.month == friday.month:
        date_thursday_str = str(thursday.day)
        date_friday_str = fmt_full(friday)
    else:
        date_thursday_str = fmt_full(thursday)
        date_friday_str = fmt_full(friday)

    if saturday.month == saturday.month: # fixed identical month condition to original behavior gracefully
        date_saturday_str = str(saturday.day)
        date_sunday_str = fmt_full(sunday)
    if saturday.month != sunday.month:
        date_saturday_str = fmt_full(saturday)
        date_sunday_str = fmt_full(sunday)

    return {
        "{date_monday}": date_monday_str,
        "{date_mon}": date_monday_str,
        "{date_wednesday}": date_wednesday_str,
        "{date_wed}": date_wednesday_str,
        "{date_thursday}": date_thursday_str,
        "{date_thur}": date_thursday_str,
        "{date_thu}": date_thursday_str,
        "{date_friday}": date_friday_str,
        "{date_fri}": date_friday_str,
        "{date_saturday}": date_saturday_str,
        "{date_sat}": date_saturday_str,
        "{date_sunday}": date_sunday_str,
        "{date_sun}": date_sunday_str,
    }

def push_to_google_slides(slide_id, tier, vehicle, processed_images_list, log_box):
    creds = get_slides_drive_oauth_creds()
    slides_service = build('slides', 'v1', credentials=creds)
    drive_service = build('drive', 'v3', credentials=creds)

    copy_name = f"Greentable Gems - {tier} {vehicle} - {time.strftime('%Y-%m-%d %H:%M:%S')}"
    copied_file = drive_service.files().copy(
        fileId=slide_id, body={'name': copy_name}
    ).execute()
    slide_id = copied_file.get('id')
    log_box.write(f"ðŸ“„ New presentation copy created: **{copy_name}**")
    
    requests_body = []
    
    requests_body.append({
        'replaceAllText': {
            'containsText': {'text': '{Tier}', 'matchCase': True},
            'replaceText': tier
        }
    })
    requests_body.append({
        'replaceAllText': {
            'containsText': {'text': '{Vehicle}', 'matchCase': True},
            'replaceText': vehicle
        }
    })
    
    if processed_images_list:
        any_date = processed_images_list[0]['date_obj']
        date_replacements = get_slide_date_replacements(any_date)
        for placeholder, replacement_value in date_replacements.items():
            requests_body.append({
                'replaceAllText': {
                    'containsText': {'text': placeholder, 'matchCase': True},
                    'replaceText': replacement_value
                }
            })
            
    day_to_placeholder_map = {
        'monday': '{IMG_mon}',
        'tuesday': '{IMG_tues}',
        'wednesday': '{IMG_wed}',
        'thursday': '{IMG_thur}',
        'friday': '{IMG_fri}',
        'saturday': '{IMG_sat}',
        'sunday': '{IMG_sun}'
    }
    
    uploaded_drive_file_ids = []
    
    for item in processed_images_list:
        day_str = item['day'].lower()
        placeholder_shape_text = day_to_placeholder_map.get(day_str)
        
        if not placeholder_shape_text:
            continue
            
        log_box.write(f"ðŸ“¤ Preparing & uploading the **{item['day']}** screenshot to the cloud server...")
        
        file_metadata = {'name': f"temp_slide_{day_str}.jpg", 'mimeType': 'image/jpeg'}
        media = MediaFileUpload(item['path'], mimetype='image/jpeg')
        uploaded_file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        file_id = uploaded_file.get('id')
        uploaded_drive_file_ids.append(file_id)
        
        drive_service.permissions().create(
            fileId=file_id,
            body={'type': 'anyone', 'role': 'reader'}
        ).execute()
        
        direct_image_url = f"https://docs.google.com/uc?export=download&id={file_id}"
        
        requests_body.append({
            'replaceAllShapesWithImage': {
                'imageUrl': direct_image_url,
                'imageReplaceMethod': 'CENTER_INSIDE',
                'containsText': {'text': placeholder_shape_text, 'matchCase': True}
            }
        })
        
    if requests_body:
        log_box.write("âš™ï¸ Updating text and inserting screenshots into the Google Slides copy...")
        slides_service.presentations().batchUpdate(
            presentationId=slide_id,
            body={'requests': requests_body}
        ).execute()
        
    return uploaded_drive_file_ids, slide_id

# ==========================================
# 6. MAIN UI (STREAMLIT APPS)
# ==========================================
st.title("ðŸŸ¢ GNS Greentable & Gems Automator")
st.caption("Driver incentive extraction automation & Google Sheets / Google Slides sync app.")

try:
    sh = get_spreadsheet()
    st.success(f"âœ… Connected to the main spreadsheet: **{sh.title}**")
except Exception as e:
    st.error(f"âŒ Failed to connect to the spreadsheet. Check secrets `gcp_service_account`. Error: {e}")
    st.stop()

reader = load_ocr_engine()

st.sidebar.header("âš™ï¸ API Settings")
gemini_api_key = st.sidebar.text_input("Gemini API Key (for fallback & Tambang Puncak & Gems)", type="password")
st.sidebar.caption(f"Greentable target tabs:\n- `{TAB_MINGGUAN}`\n- `{TAB_PUNCAK}`")

st.divider()

uploaded_image = st.file_uploader(
    "Upload Greentable weekly incentive image (full image)",
    type=["jpg", "jpeg", "png"],
)

image_path = None
if uploaded_image is not None:
    os.makedirs("temp_uploads", exist_ok=True)
    image_path = os.path.join("temp_uploads", uploaded_image.name)
    with open(image_path, "wb") as f:
        f.write(uploaded_image.getbuffer())
    st.image(image_path, caption="Greentable Image Preview", width=350)

st.divider()

tab_mingguan, tab_puncak, tab_gems, tab_compare = st.tabs(
    ["ðŸ“… Weekly & Daily Bonus", "â›°ï¸ Tambang Puncak", "ðŸ’Ž Gems Automator", "ðŸ”„ Compare This Week vs Last Week"]
)

# ------------------------------------------
# TAB 1 â€” WEEKLY & DAILY BONUS
# ------------------------------------------
with tab_mingguan:
    st.subheader("Weekly Bonus & Daily Bonus")
    if image_path is None:
        st.info("Upload an image above first to start extraction.")
    else:
        if st.button("ðŸš€ Run Extraction", key="run_mingguan"):
            log_box = st.container()
            try:
                bonus_mingguan, bonus_harian = extract_mingguan(
                    reader, image_path, gemini_api_key, log=log_box.write
                )
                df_mingguan = build_sheet_rows_mingguan(bonus_mingguan, bonus_harian)
                st.session_state["df_mingguan"] = df_mingguan
                st.success(f"Extraction finished â€” {len(df_mingguan)} rows ready to upload.")
            except Exception as e:
                st.error(f"âŒ Extraction failed: {e}")

        if "df_mingguan" in st.session_state:
            st.dataframe(st.session_state["df_mingguan"], use_container_width=True)
            if st.button("ðŸ“¤ Upload to Google Sheets", key="upload_mingguan"):
                ok = upload_to_gsheets(st.session_state["df_mingguan"], TAB_MINGGUAN)
                if ok:
                    st.success(f"ðŸŽ‰ {len(st.session_state['df_mingguan'])} rows sent to '{TAB_MINGGUAN}'.")

# ------------------------------------------
# TAB 2 â€” TAMBANG PUNCAK
# ------------------------------------------
with tab_puncak:
    st.subheader("Tambang Puncak (Peak Fare)")
    if image_path is None:
        st.info("Upload an image above first to start extraction.")
    elif not gemini_api_key:
        st.warning("âš ï¸ This module uses Gemini Vision (2-pass) â€” enter the Gemini API Key in the sidebar first.")
    else:
        if st.button("ðŸš€ Run Extraction", key="run_puncak"):
            log_box = st.container()
            try:
                raw_puncak = extract_peak_fare(image_path, gemini_api_key, log=log_box.write)
                df_puncak = process_raw_data_puncak(raw_puncak)
                st.session_state["df_puncak"] = df_puncak
                st.success(f"Extraction finished â€” {len(df_puncak)} rows ready to upload.")
            except Exception as e:
                st.error(f"âŒ Extraction failed: {e}")

        if "df_puncak" in st.session_state:
            st.dataframe(st.session_state["df_puncak"], use_container_width=True)
            if st.button("ðŸ“¤ Upload to Google Sheets", key="upload_puncak"):
                ok = upload_to_gsheets(st.session_state["df_puncak"], TAB_PUNCAK)
                if ok:
                    st.success(f"ðŸŽ‰ {len(st.session_state['df_puncak'])} rows sent to '{TAB_PUNCAK}'.")

# ------------------------------------------
# TAB 3 â€” GEMS AUTOMATOR
# ------------------------------------------
with tab_gems:
    st.subheader("ðŸ’Ž Gems Extraction Automation & Google Slides Sync")
    st.caption("Upload mission screenshot â†’ extract numbers to GSheets â†’ Auto-generate a Google Slides presentation.")

    try:
        sheet_this_week = get_gems_sheet()
        st.success("âœ… Connected to the Gems Spreadsheet!")
    except Exception as e:
        st.error(f"âŒ Failed to open the Gems spreadsheet. Error: {e}")
        st.stop()

    col_a, col_b = st.columns(2)
    tier_options = ["Diamond", "Sapphire", "Ruby", "Emerald"]
    vehicle_options = ["Walker", "Motorcycle", "E-bike", "Bicycle"]
    selected_tier = col_a.selectbox("Select Tier", tier_options, key="gems_tier")
    selected_vehicle = col_b.selectbox("Select Vehicle", vehicle_options, key="gems_vehicle")

    target_cells = get_target_cells(selected_tier, selected_vehicle)
    target_display = [f"{t}-{v}" for t, v in target_cells]
    st.info(f"ðŸ“Œ **Main GSheets target:** {selected_tier} - {selected_vehicle}\n\n"
            f"ðŸ”„ **Auto-fill GSheets to:** {', '.join(target_display)}\n\n"
            f"ðŸ“Š **Slides sync target:** Only the uploaded option (exclusive).")

    gems_files = st.file_uploader(
        "Upload Mission Images (batch / multiple at once)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        key="gems_uploader",
    )

    if gems_files and st.button("ðŸš€ Run Extraction & Update Entire System", type="primary", key="run_gems"):
        temp_dir = "temp_uploads_gems"
        os.makedirs(temp_dir, exist_ok=True)

        all_rows_this_week = sheet_this_week.get_all_values()
        progress_bar = st.progress(0)
        
        slides_payload_list = []

        for idx, uploaded_file in enumerate(gems_files):
            filename = uploaded_file.name

            with st.expander(f"âš™ï¸ Processing: {filename}", expanded=True):
                temp_path = os.path.join(temp_dir, filename)
                with open(temp_path, "wb") as f:
                    f.write(uploaded_file.getbuffer())

                img = Image.open(temp_path)
                if img.width > img.height:
                    st.write("ðŸ”„ Rotating landscape image upright...")
                    img_rotated = img.rotate(90, expand=True)
                    temp_path = os.path.join(temp_dir, "prepared_" + filename)
                    img_rotated.save(temp_path)

                try:
                    result = reader.readtext(temp_path, detail=0)
                except Exception as e:
                    st.warning(f"âš ï¸ OCR reading error ({e}). Proceeding to AI...")
                    result = []
                full_text = " ".join(result)

                formatted_date, extracted_day, current_dt_obj = None, None, None
                fname_match = re.search(r"(\d{1,2})[\s_]*([A-Za-z]{3,9})", os.path.splitext(filename)[0])

                if fname_match:
                    try:
                        current_dt_obj = datetime.strptime(f"{fname_match.group(1)} {fname_match.group(2)[:3].capitalize()} 2026", "%d %b %Y")
                        extracted_day = current_dt_obj.strftime("%A")
                        formatted_date = current_dt_obj.strftime("%d/%m/%Y")
                    except Exception:
                        pass

                if not formatted_date:
                    date_match = re.search(r"(\d{1,2})\s*([A-Za-z]{3,9})\s*(\d{4})?", full_text)
                    if date_match:
                        try:
                            tahun_angka = date_match.group(3) if date_match.group(3) else "2026"
                            current_dt_obj = datetime.strptime(f"{date_match.group(1)} {date_match.group(2)[:3].capitalize()} {tahun_angka}", "%d %b %Y")
                            extracted_day = current_dt_obj.strftime("%A")
                            formatted_date = current_dt_obj.strftime("%d/%m/%Y")
                        except Exception:
                            pass

                gems_found, rewards_found = [], []

                if not formatted_date:
                    st.warning("âš ï¸ Date not detected from OCR text. Redirecting to AI to scan the date and data...")
                    gems_found, rewards_found, ai_date = extract_via_gemini_gems(temp_path, gemini_api_key)
                    
                    if ai_date:
                        try:
                            current_dt_obj = datetime.strptime(ai_date, "%d %b %Y")
                            extracted_day = current_dt_obj.strftime("%A")
                            formatted_date = current_dt_obj.strftime("%d/%m/%Y")
                            st.write(f"ðŸ“… Date & Day Detected (AI): **{formatted_date} ({extracted_day})**")
                        except Exception:
                            pass

                    if not formatted_date:
                        st.error("âš ï¸ Date still not detected by AI. File skipped.")
                        continue
                else:
                    st.write(f"ðŸ“… Date & Day Detected: **{formatted_date} ({extracted_day})**")
                    
                    gems_found, rewards_found, _ = extract_gems_rewards(result)

                    if len(gems_found) != 3 or len(rewards_found) != 3:
                        st.warning("âš ï¸ Normal OCR reading failed. Trying AI fallback directly...")
                        gems_found, rewards_found, _ = extract_via_gemini_gems(temp_path, gemini_api_key)

                if len(gems_found) == 3 and len(rewards_found) == 3:
                    pairs = list(zip(gems_found, rewards_found))
                    slides_payload_list.append({
                        'day': extracted_day,
                        'path': temp_path,
                        'date_obj': current_dt_obj
                    })
                else:
                    st.error("âŒ Number extraction failed completely for this file (even with AI). Please check manually.")
                    continue

                # --- 1. GOOGLE SHEETS UPDATE PROCESS ---
                target_day = extracted_day.lower()
                current_target_cells = get_target_cells(selected_tier, selected_vehicle)

                for cell_tier, cell_vehicle in current_target_cells:
                    matched_row_indices = []
                    for r_idx, row in enumerate(all_rows_this_week):
                        if r_idx == 0:
                            continue
                        r_tier = row[0].strip().lower() if len(row) > 0 else ""
                        r_veh = row[1].strip().lower() if len(row) > 1 else ""
                        r_day = row[2].strip().lower() if len(row) > 2 else ""
                        if r_day == "wedesday":
                            r_day = "wednesday"

                        if r_tier == cell_tier.lower() and r_veh == cell_vehicle.lower() and r_day == target_day:
                            matched_row_indices.append(r_idx + 1)

                    if len(matched_row_indices) >= 3:
                        update_payload = []
                        for i in range(3):
                            g_val, r_val = pairs[i][0], pairs[i][1]
                            inc = round(r_val / g_val, 2) if g_val > 0 else 0
                            update_payload.append([formatted_date, g_val, round(r_val, 2), round(inc, 2)])

                        start_row, end_row = matched_row_indices[0], matched_row_indices[2]
                        sheet_this_week.update(
                            values=update_payload,
                            range_name=f"D{start_row}:G{end_row}",
                            value_input_option="USER_ENTERED",
                        )

                        if cell_tier.lower() == selected_tier.lower() and cell_vehicle.lower() == selected_vehicle.lower():
                            st.success(f"âœ¨ Data **{cell_tier}-{cell_vehicle}** written to Spreadsheet rows {start_row}-{end_row}.")
                        else:
                            st.info(f"âž¡ï¸ Twin data **{cell_tier}-{cell_vehicle}** auto-filled in rows {start_row}-{end_row}.")
                    else:
                        st.error(f"âŒ Empty rows for {cell_tier}-{cell_vehicle} on {extracted_day} not found.")

            progress_bar.progress((idx + 1) / len(gems_files))


        # --- 2. GOOGLE SLIDES UPDATE PROCESS ---
        if slides_payload_list:
            st.divider()
            st.subheader("ðŸ“Š Starting Google Slides Presentation Sync...")
            log_slides = st.container()
            try:
                log_slides.write("ðŸ“ **Processing First Slide Template...**")
                uploaded_ids, new_presentation_id = push_to_google_slides(
                    slide_id=GEMS_SLIDES_ID,
                    tier=selected_tier,
                    vehicle=selected_vehicle,
                    processed_images_list=slides_payload_list,
                    log_box=log_slides
                )
                
                log_slides.write("ðŸ“ **Processing Second Slide Template...**")
                uploaded_ids_2, new_presentation_id_2 = push_to_google_slides(
                    slide_id=GEMS_SLIDES_ID_2,
                    tier=selected_tier,
                    vehicle=selected_vehicle,
                    processed_images_list=slides_payload_list,
                    log_box=log_slides
                )
                
                st.success("ðŸŽ‰ Both Google Slides copies created & updated successfully!")
                st.info(f"ðŸ”— Result Presentation Link (Template 1): [Buka Google Slides](https://docs.google.com/presentation/d/{new_presentation_id}/edit)")
                st.info(f"ðŸ”— Result Presentation Link (Template 2): [Buka Google Slides](https://docs.google.com/presentation/d/{new_presentation_id_2}/edit)")
            except Exception as slide_err:
                st.error(f"âš ï¸ Failed to update Google Slides: {slide_err}")
        
        st.balloons()
        st.success("ðŸŽ‰ The full Sheets data automation and Slides visualization pipeline is complete!")

# ------------------------------------------
# TAB 4 â€” COMPARE THIS WEEK VS LAST WEEK
# ------------------------------------------
with tab_compare:
    st.subheader("ðŸ”„ Compare This Week vs Last Week")
    st.caption(
        "Upload this week's & last week's Greentable images. Both will be extracted "
        "(using the same Bonus Mingguan & Tambang Puncak logic), then compared "
        "automatically into copy-ready report text. No data is uploaded to Google Sheets."
    )

    col_this, col_last = st.columns(2)
    with col_this:
        st.markdown("**ðŸ“… This Week Image**")
        img_this_file = st.file_uploader(
            "Upload this week's image", type=["jpg", "jpeg", "png"], key="compare_this_week_uploader"
        )
        if img_this_file is not None:
            st.image(img_this_file, caption="Preview This Week", width=280)
    with col_last:
        st.markdown("**ðŸ“† Last Week Image**")
        img_last_file = st.file_uploader(
            "Upload last week's image", type=["jpg", "jpeg", "png"], key="compare_last_week_uploader"
        )
        if img_last_file is not None:
            st.image(img_last_file, caption="Preview Last Week", width=280)

    if not gemini_api_key:
        st.warning("âš ï¸ This module needs the Gemini API Key in the sidebar (used for Tambang Puncak & Bonus Mingguan fallback).")

    run_compare_disabled = not (img_this_file and img_last_file and gemini_api_key)
    if st.button("ðŸš€ Extract & Compare", key="run_compare", disabled=run_compare_disabled):
        compare_temp_dir = "temp_uploads_compare"
        os.makedirs(compare_temp_dir, exist_ok=True)

        path_this = os.path.join(compare_temp_dir, "this_week_" + img_this_file.name)
        with open(path_this, "wb") as f:
            f.write(img_this_file.getbuffer())

        path_last = os.path.join(compare_temp_dir, "last_week_" + img_last_file.name)
        with open(path_last, "wb") as f:
            f.write(img_last_file.getbuffer())

        try:
            with st.status("â³ Extracting This Week image...", expanded=True) as status_this:
                mingguan_this, harian_this = extract_mingguan(reader, path_this, gemini_api_key, log=st.write)
                raw_puncak_this = extract_peak_fare(path_this, gemini_api_key, log=st.write)
                df_puncak_this = process_raw_data_puncak(raw_puncak_this)
                status_this.update(label="âœ… This Week extraction complete.", state="complete")

            with st.status("â³ Extracting Last Week image...", expanded=True) as status_last:
                mingguan_last, harian_last = extract_mingguan(reader, path_last, gemini_api_key, log=st.write)
                raw_puncak_last = extract_peak_fare(path_last, gemini_api_key, log=st.write)
                df_puncak_last = process_raw_data_puncak(raw_puncak_last)
                status_last.update(label="âœ… Last Week extraction complete.", state="complete")

            report_text = build_full_comparison_report(
                mingguan_this, harian_this, df_puncak_this,
                mingguan_last, harian_last, df_puncak_last,
            )
            st.session_state["compare_report_text"] = report_text
            st.success("ðŸŽ‰ Comparison done! Report ready to copy below.")
        except Exception as e:
            st.error(f"âŒ Failed to process comparison: {e}")

    if "compare_report_text" in st.session_state:
        st.divider()
        st.markdown("### ðŸ“‹ Copy-Ready Message")
        st.code(st.session_state["compare_report_text"], language="markdown")
