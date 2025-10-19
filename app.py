# app.py
# -*- coding: utf-8 -*-
import os, re, asyncio
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from linebot import LineBotApi, WebhookParser
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ===== ENV =====
load_dotenv()
WTMS_USER = os.getenv("WTMS_USER", "")
WTMS_PASS = os.getenv("WTMS_PASS", "")

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")  # ผู้รับแจ้ง default
AUTO_NOTIFY_GREEN = os.getenv("AUTO_NOTIFY_GREEN", "0") == "1"

BASE_URL = (os.getenv("BASE_URL") or "").rstrip("/")
HEADLESS = os.getenv("HEADLESS", "1") == "1"
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "30000"))

WTMS_URL = "https://wtms.pwa.co.th/"
WTMS_APP = "https://wtms.pwa.co.th/app.html"
DMAMA_ANAL = "https://dmama.pwa.co.th/app/#/analysis/normal"

ACK_TEXTS   = ["รับทราบข้อมูล", "รับทราบ", "ยืนยันรับทราบ"]
DMAMA_TEXTS = ["เข้าระบบ Dmama", "เข้าสู่ระบบ Dmama", "Dmama", "DMAMA"]
SELECT_ALL  = "เลือกทั้งหมด"
OK_TEXT     = "ตกลง"
SEARCH_TEXT = "ค้นหา"

SHOTS_DIR = Path("shots")
SHOTS_DIR.mkdir(parents=True, exist_ok=True)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
parser = WebhookParser(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

app = FastAPI()
app.mount("/shots", StaticFiles(directory=str(SHOTS_DIR), html=False), name="shots")

# ---------- helpers ----------
async def _snap(page, prefix: str) -> str:
    fname = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    fpath = SHOTS_DIR / fname
    await page.screenshot(path=str(fpath), full_page=True)
    return f"{BASE_URL}/shots/{fname}" if BASE_URL else str(fpath)

async def _login_wtms(page):
    await page.goto(WTMS_URL, wait_until="domcontentloaded")
    await page.fill("#username", WTMS_USER)
    await page.fill("#password", WTMS_PASS)
    try:
        await page.get_by_role("button", name=re.compile("เข้าสู่ระบบ")).click()
    except Exception:
        await page.click('button[type="submit"], input[type="submit"]')
    try:
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
    except PWTimeout:
        pass

async def _click_optional_ack(page):
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
    dmama = page
    try:
        async with context.expect_page() as newp:
            clicked = False
            for text in DMAMA_TEXTS:
                if not clicked:
                    try:
                        await page.get_by_role("button", name=re.compile(text)).click(timeout=5000); clicked=True; break
                    except Exception:
                        try:
                            await page.get_by_role("link", name=re.compile(text)).click(timeout=5000); clicked=True; break
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
            count = await okbtns.count()
            if count > 0: await okbtns.nth(count - 1).click()
            else: await dmama.locator(f'button:has-text("{OK_TEXT}")').last.click()
        except Exception:
            pass
    try:
        await dmama.get_by_role("button", name=re.compile(SEARCH_TEXT)).click(timeout=2500)
    except Exception:
        pass
    return dmama

def _parse_green_summary(html: str) -> str:
    """
    ดึงเวลารับทราบ WTMS และบรรทัด “มีการใช้งานระบบ DMAMA ครบตามเงื่อนไขแล้ว”
    เพื่อใช้สรุปสั้น ๆ
    """
    # หา timestamp ในรูปแบบ 18/10/2568 07:54:01 หรือคล้ายกัน
    ts = None
    m = re.search(r"รับทราบข้อมูล\s*WTMS\s*เมื่อ\s*([\d/]+\s+\d{1,2}:\d{2}:\d{2})", html)
    if m: ts = m.group(1)
    line2 = "มีการใช้งานระบบ DMAMA ครบตามเงื่อนไขแล้ว"
    if ts:
        return f"รับทราบ WTMS เมื่อ {ts}\n{line2}"
    return line2

async def _verify_green_status(page):
    await page.goto(WTMS_APP, wait_until="domcontentloaded")
    await page.wait_for_timeout(800)
    await page.reload(wait_until="domcontentloaded")
    html = await page.content()
    ok = ("รับทราบข้อมูล WTMS เมื่อ" in html) and ("มีการใช้งานระบบ DMAMA ครบตามเงื่อนไขแล้ว" in html)
    return ok, html

# ---------- main flow ----------
async def run_wtms_flow() -> dict:
    shots = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
            context = await browser.new_context(viewport={"width": 1366, "height": 900})
            page = await context.new_page()
            page.set_default_timeout(TIMEOUT_MS)

            await _login_wtms(page)
            shots.append(await _snap(page, "01_login_ok"))

            try:
                acked = await _click_optional_ack(page)
                if acked: await page.wait_for_timeout(400)
            except Exception:
                pass

            dmama = await _open_dmama_and_select_all(context, page)
            shots.append(await _snap(dmama, "02_dmama_analysis"))

            ok, html = await _verify_green_status(page)
            shots.append(await _snap(page, "03_wtms_status"))

            await context.close(); await browser.close()
            summary = _parse_green_summary(html) if ok else None
            return {"ok": ok, "shots": shots, "summary": summary}
    except Exception as e:
        return {"ok": False, "shots": shots, "error": str(e)}

# ---------- endpoints ----------
@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/callback", response_class=PlainTextResponse)
async def callback(request: Request):
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
                    line_bot_api.reply_message(ev.reply_token,
                        TextSendMessage(text="🕐 รับคำสั่งแล้ว กำลังตรวจ WTMS/DMAMA…"))
                except Exception:
                    pass

                async def worker(user_id: str):
                    r = await run_wtms_flow()
                    ok, shots, summary = bool(r.get("ok")), r.get("shots", []), r.get("summary")
                    if ok:
                        msgs = [TextSendMessage(text=f"✅ เงื่อนไขครบ\n{summary}")]
                        for u in shots: msgs.append(ImageSendMessage(u, u))
                        line_bot_api.push_message(user_id, msgs)
                        # แจ้ง default เพิ่ม (กรณีอยากแจ้งห้องกลาง) เมื่อ AUTO_NOTIFY_GREEN=1 และ user_id ไม่ใช่ LINE_USER_ID
                        if AUTO_NOTIFY_GREEN and LINE_USER_ID and LINE_USER_ID != user_id:
                            try:
                                line_bot_api.push_message(LINE_USER_ID, msgs)
                            except Exception:
                                pass
                    else:
                        # สถานะยังไม่ครบ → ส่งเฉพาะข้อความสั้น ๆ และแนบรูปสุดท้าย (ถ้าต้องการ)
                        msgs = [TextSendMessage(text="⚠️ ยังไม่ครบเงื่อนไข")]
                        if shots:
                            msgs.append(ImageSendMessage(shots[-1], shots[-1]))
                        line_bot_api.push_message(user_id, msgs)

                asyncio.create_task(worker(ev.source.user_id))
    return "OK"

# เรียกผ่านเบราว์เซอร์หรือ Render “Manual Trigger”
@app.get("/trigger")
async def trigger():
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
        line_bot_api.push_message(LINE_USER_ID, TextSendMessage(text="⚠️ ยังไม่ครบเงื่อนไข"))
        return JSONResponse({"ok": False, "notified": True})
