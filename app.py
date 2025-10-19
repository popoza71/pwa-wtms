# app.py
# -*- coding: utf-8 -*-
import os, re, json, asyncio
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from linebot import LineBotApi, WebhookParser
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ===================== ENV & CONST =====================
load_dotenv()

WTMS_USER = os.getenv("WTMS_USER", "")
WTMS_PASS = os.getenv("WTMS_PASS", "")

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")  # ผู้รับ default สำหรับ /trigger หรือ auto-notify
AUTO_NOTIFY_GREEN = os.getenv("AUTO_NOTIFY_GREEN", "0") == "1"

BASE_URL = (os.getenv("BASE_URL") or "").rstrip("/")
HEADLESS = os.getenv("HEADLESS", "1") == "1"
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "30000"))
DEBUG = os.getenv("DEBUG", "0") == "1"

WTMS_URL  = "https://wtms.pwa.co.th/"
WTMS_APP  = "https://wtms.pwa.co.th/app.html"
DMAMA_ANAL= "https://dmama.pwa.co.th/app/#/analysis/normal"

ACK_TEXTS   = ["รับทราบข้อมูล", "รับทราบ", "ยืนยันรับทราบ"]
DMAMA_TEXTS = ["เข้าระบบ Dmama", "เข้าสู่ระบบ Dmama", "Dmama", "DMAMA"]
SELECT_ALL  = "เลือกทั้งหมด"
OK_TEXT     = "ตกลง"
SEARCH_TEXT = "ค้นหา"

SHOTS_DIR = Path("shots")
SHOTS_DIR.mkdir(parents=True, exist_ok=True)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
parser       = WebhookParser(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

app = FastAPI()
app.mount("/shots", StaticFiles(directory=str(SHOTS_DIR), html=False), name="shots")


# ===================== Helpers =====================
async def _login_wtms(page):
    await page.goto(WTMS_URL, wait_until="domcontentloaded")
    # เผื่อมี redirect/โหลดช้า
    try:
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
    except PWTimeout:
        pass

    # ลองหาช่อง "ผู้ใช้" ด้วยหลาย selector
    user_locators = [
        '#username',
        'input[name="username"]',
        'input[type="text"]',
        'input[placeholder*="รหัสพนักงาน"]',
        'input[placeholder*="ผู้ใช้"]',
    ]
    pass_locators = [
        '#password',
        'input[name="password"]',
        'input[type="password"]',
        'input[placeholder*="รหัสผ่าน"]',
    ]

    # หา field ผู้ใช้
    for sel in user_locators:
        try:
            await page.wait_for_selector(sel, timeout=8000)
            await page.fill(sel, WTMS_USER)
            break
        except Exception:
            continue
    else:
        # ไม่เจอช่องผู้ใช้ -> เก็บหลักฐานแล้วโยน error ออกไป
        html = await page.content()
        (SHOTS_DIR / "login_page.html").write_text(html, encoding="utf-8")
        await page.screenshot(path=str(SHOTS_DIR / "login_page.png"), full_page=True)
        raise RuntimeError("ไม่พบช่องกรอกผู้ใช้บนหน้า WTMS (บันทึกเป็น login_page.* แล้ว)")

    # หา field รหัสผ่าน
    for sel in pass_locators:
        try:
            await page.wait_for_selector(sel, timeout=8000)
            await page.fill(sel, WTMS_PASS)
            break
        except Exception:
            continue

    # ปุ่มเข้าสู่ระบบหลายแบบ
    login_buttons = [
        'button:has-text("เข้าสู่ระบบ")',
        'input[type="submit"]',
        'button[type="submit"]',
        'button:has-text("Login")',
    ]
    clicked = False
    for sel in login_buttons:
        try:
            await page.click(sel, timeout=4000)
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        # ลองกด enter ในช่อง password
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass

    # รอให้เข้าสู่ระบบเสร็จ
    try:
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
    except PWTimeout:
        pass


async def _click_optional_ack(page) -> bool:
    """ถ้ามีปุ่ม 'รับทราบข้อมูล' ให้กด"""
    for text in ACK_TEXTS:
        try:
            await page.get_by_role("button", name=re.compile(text)).click(timeout=3500)
            return True
        except Exception:
            try:
                await page.locator(f'button:has-text("{text}")').first.click(timeout=2000)
                return True
            except Exception:
                continue
    return False

async def _open_dmama_and_select_all(context, page):
    """เข้าหน้า Analysis ของ DMAMA → เลือกทั้งหมด → ตกลง → (ค้นหา)"""
    dmama = page
    try:
        async with context.expect_page() as newp:
            clicked = False
            for text in DMAMA_TEXTS:
                if clicked: break
                try:
                    await page.get_by_role("button", name=re.compile(text)).click(timeout=5000)
                    clicked = True
                except Exception:
                    try:
                        await page.get_by_role("link", name=re.compile(text)).click(timeout=5000)
                        clicked = True
                    except Exception:
                        continue
            if not clicked:
                await page.locator(':is(button,a):has-text("Dmama")').first.click(timeout=5000)
        dmama = await newp.value
    except PWTimeout:
        dmama = page

    try:
        await dmama.goto(DMAMA_ANAL, wait_until="domcontentloaded")
    except Exception:
        pass
    await dmama.wait_for_timeout(800)

    opened = False
    for sel in [
        'input[placeholder*="รายการมาตรวัด"]',
        'div:has-text("รายการมาตรวัด") input',
        'text=รายการมาตรวัด'
    ]:
        try:
            await dmama.click(sel, timeout=4000); opened=True; break
        except Exception:
            continue

    if opened:
        try:
            await dmama.get_by_label(SELECT_ALL, exact=False).check(timeout=4000)
        except Exception:
            try:
                await dmama.locator(f'label:has-text("{SELECT_ALL}")').click(timeout=4000)
            except Exception:
                pass
        await dmama.wait_for_timeout(300)
        try:
            okbtns = dmama.get_by_role("button", name=re.compile(OK_TEXT))
            cnt = await okbtns.count()
            if cnt > 0: await okbtns.nth(cnt-1).click()
            else: await dmama.locator(f'button:has-text("{OK_TEXT}")').last.click()
        except Exception:
            pass

    try:
        await dmama.get_by_role("button", name=re.compile(SEARCH_TEXT)).click(timeout=2500)
    except Exception:
        pass
    return dmama

# ---- ตรวจสีเขียว (พร้อมดีบั๊ก) ----
def _contains_any(hay: str, needles: list[str]) -> bool:
    return any(n in hay for n in needles)

GREEN_NEEDLE_1 = [
    "รับทราบข้อมูล WTMS เมื่อ",
    "รับทราบ ข้อมูล WTMS เมื่อ",   # กันเว้นวรรคแปลก ๆ
]
GREEN_NEEDLE_2 = [
    "มีการใช้งานระบบ DMAMA ครบตามเงื่อนไขแล้ว",
]

async def _verify_green_status_and_dump(page):
    """เปิด app.html → reload → ตรวจ และบันทึก HTML ลงไฟล์เพื่อดีบั๊ก"""
    await page.goto(WTMS_APP, wait_until="domcontentloaded")
    await page.wait_for_timeout(1200)
    await page.reload(wait_until="networkidle")

    html = await page.content()
    # เก็บไฟล์ HTML ไว้เปิดดูภายหลัง
    html_name = f"app_html_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    html_path = SHOTS_DIR / html_name
    try:
        html_path.write_text(html, encoding="utf-8")
    except Exception:
        pass

    # normalize ช่องว่าง
    norm = html.replace("\xa0", " ").replace("&nbsp;", " ")
    norm = re.sub(r"\s+", " ", norm)

    ok1 = _contains_any(norm, GREEN_NEEDLE_1)
    ok2 = _contains_any(norm, GREEN_NEEDLE_2)
    ok  = bool(ok1 and ok2)

    ts = None
    m = re.search(r"รับทราบข้อมูล\s*WTMS\s*เมื่อ\s*([\d/]+\s+\d{1,2}:\d{2}:\d{2})", norm)
    if m: ts = m.group(1)

    dbg = {
        "ok1_found": ok1,
        "ok2_found": ok2,
        "timestamp": ts,
        "html_file": f"{BASE_URL}/shots/{html_name}" if BASE_URL else str(html_path),
        "snippet": norm[:600],
    }
    return ok, ts, dbg


# ===================== Main Flow =====================
async def run_wtms_flow() -> dict:
    """
    ทำงานครบ:
      - Login WTMS
      - กด 'รับทราบข้อมูล' (ถ้ามี)
      - เข้าระบบ DMAMA → Analysis → เลือกทั้งหมด → ตกลง → (ค้นหา)
      - เปิด WTMS app.html → reload → ตรวจว่าขึ้นสีเขียวครบ
    คืนค่า: {"ok": bool, "shots": [url,...], "summary": str|None, "debug": dict?}
    """
    shots = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
            context = await browser.new_context(viewport={"width": 1366, "height": 900})
            page = await context.new_page()
            page.set_default_timeout(TIMEOUT_MS)

            # 1) Login
            await _login_wtms(page)
            shots.append(await _snap(page, "01_login_ok"))

            # 2) รับทราบถ้ามี
            try:
                if await _click_optional_ack(page):
                    await page.wait_for_timeout(400)
            except Exception:
                pass

            # 3) DMAMA → เลือกทั้งหมด
            dmama = await _open_dmama_and_select_all(context, page)
            shots.append(await _snap(dmama, "02_dmama_analysis"))

            # 4) ตรวจหน้า app.html
            ok, ts, dbg = await _verify_green_status_and_dump(page)
            shots.append(await _snap(page, "03_wtms_status"))

            if DEBUG:
                print("DEBUG::WTMS", json.dumps(dbg, ensure_ascii=False))

            await context.close(); await browser.close()

            summary = (f"รับทราบ WTMS เมื่อ {ts}\nมีการใช้งานระบบ DMAMA ครบตามเงื่อนไขแล้ว") if ok and ts else None
            return {"ok": ok, "shots": shots, "summary": summary, "debug": dbg if DEBUG else None}

    except Exception as e:
        return {"ok": False, "shots": shots, "error": str(e)}


# ===================== FastAPI Endpoints =====================
@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/debug-run")
async def debug_run():
    """รันทดสอบครั้งเดียวและคืนผล JSON (ไม่ส่งเข้า LINE)"""
    r = await run_wtms_flow()
    return JSONResponse(r)

@app.get("/trigger")
async def trigger():
    """ทริกเกอร์รัน 1 ครั้งและ push ผลไป LINE_USER_ID"""
    if not line_bot_api or not LINE_USER_ID:
        raise HTTPException(status_code=500, detail="Missing LINE credentials/LINE_USER_ID")
    r = await run_wtms_flow()
    ok, shots, summary = bool(r.get("ok")), r.get("shots", []), r.get("summary")
    if ok:
        msgs = [TextSendMessage(text=f"✅ เงื่อนไขครบ\n{summary}")]
        for u in shots: msgs.append(ImageSendMessage(u, u))
        line_bot_api.push_message(LINE_USER_ID, msgs)
        return JSONResponse({"ok": True, "notified": True, "summary": summary})
    else:
        msg = "⚠️ ยังไม่ครบเงื่อนไข"
        if r.get("error"): msg += f"\n{r['error']}"
        line_bot_api.push_message(LINE_USER_ID, TextSendMessage(text=msg))
        if shots: line_bot_api.push_message(LINE_USER_ID, ImageSendMessage(shots[-1], shots[-1]))
        return JSONResponse({"ok": False, "notified": True, "error": r.get("error")})

@app.post("/callback", response_class=PlainTextResponse)
async def callback(request: Request):
    """LINE Webhook"""
    if not parser or not line_bot_api:
        raise HTTPException(status_code=500, detail="LINE credentials are not configured")

    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8", errors="ignore")

    try:
        events = parser.parse(body, signature)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    for ev in events:
        if isinstance(ev, MessageEvent) and isinstance(ev.message, TextMessage):
            text = (ev.message.text or "").strip().lower()
            if text in ("wtms", "/wtms", "dmama", "เช็คwtms", "check"):
                # ตอบรับทันที
                try:
                    line_bot_api.reply_message(
                        ev.reply_token,
                        TextSendMessage(text="🕐 รับคำสั่งแล้ว กำลังตรวจ WTMS/DMAMA…"),
                    )
                except Exception:
                    pass

                async def worker(user_id: str):
                    r = await run_wtms_flow()
                    ok, shots, summary = bool(r.get("ok")), r.get("shots", []), r.get("summary")
                    if ok:
                        msgs = [TextSendMessage(text=f"✅ เงื่อนไขครบ\n{summary}")]
                        for u in shots: msgs.append(ImageSendMessage(u, u))
                        line_bot_api.push_message(user_id, msgs)
                        # แจ้ง default (เช่นห้องกลาง) ด้วยถ้าตั้งค่าไว้
                        if AUTO_NOTIFY_GREEN and LINE_USER_ID and LINE_USER_ID != user_id:
                            try:
                                line_bot_api.push_message(LINE_USER_ID, msgs)
                            except Exception:
                                pass
                    else:
                        msg = "⚠️ ยังไม่ครบเงื่อนไข"
                        if r.get("error"): msg += f"\n{r['error']}"
                        msgs = [TextSendMessage(text=msg)]
                        if shots:
                            msgs.append(ImageSendMessage(shots[-1], shots[-1]))
                        line_bot_api.push_message(user_id, msgs)

                asyncio.create_task(worker(ev.source.user_id))

    return "OK"

