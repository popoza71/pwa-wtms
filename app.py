# app.py
# -*- coding: utf-8 -*-
import os
import re
import asyncio
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from linebot import LineBotApi, WebhookParser
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ====== ENV & CONSTANTS ======
load_dotenv()

WTMS_USER = os.getenv("WTMS_USER")
WTMS_PASS = os.getenv("WTMS_PASS")

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET") or ""
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or ""
BASE_URL = (os.getenv("BASE_URL") or "").rstrip("/")

HEADLESS = os.getenv("HEADLESS", "1") == "1"
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "30000"))

WTMS_URL = "https://wtms.pwa.co.th/"
WTMS_APP = "https://wtms.pwa.co.th/app.html"
DMAMA_ANAL = "https://dmama.pwa.co.th/app/#/analysis/normal"

# ชื่อปุ่ม/ข้อความสำคัญ (กันกรณีมีเว้นวรรค/ตัวสะกดต่างกัน)
ACK_TEXTS   = ["รับทราบข้อมูล", "รับทราบ", "ยืนยันรับทราบ"]
DMAMA_TEXTS = ["เข้าระบบ Dmama", "เข้าสู่ระบบ Dmama", "Dmama", "DMAMA"]
SELECT_ALL  = "เลือกทั้งหมด"
OK_TEXT     = "ตกลง"
SEARCH_TEXT = "ค้นหา"

# โฟลเดอร์เก็บสกรีนช็อต
SHOTS_DIR = Path("shots")
SHOTS_DIR.mkdir(parents=True, exist_ok=True)

# ====== LINE SDK (optional at startup) ======
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
parser = WebhookParser(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

# ====== FastAPI App ======
app = FastAPI()
app.mount("/shots", StaticFiles(directory=str(SHOTS_DIR), html=False), name="shots")


# ---------- Playwright helpers ----------
async def _snap(page, prefix: str) -> str:
    """ถ่ายรูปทั้งหน้าและคืน 'URL' สำหรับส่ง LINE"""
    fname = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    fpath = SHOTS_DIR / fname
    await page.screenshot(path=str(fpath), full_page=True)
    return f"{BASE_URL}/shots/{fname}" if BASE_URL else str(fpath)


async def _login_wtms(page):
    await page.goto(WTMS_URL, wait_until="domcontentloaded")
    await page.fill("#username", WTMS_USER)
    await page.fill("#password", WTMS_PASS)
    # ปุ่มเข้าสู่ระบบ
    try:
        await page.get_by_role("button", name=re.compile("เข้าสู่ระบบ")).click()
    except Exception:
        await page.click('button[type="submit"], input[type="submit"]')
    try:
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
    except PWTimeout:
        pass


async def _click_optional_ack(page):
    # บางวันไม่มีปุ่มนี้
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
    # คลิกปุ่ม/ลิงก์ Dmama (คาดว่าเปิดแท็บใหม่)
    dmama = page
    try:
        async with context.expect_page() as newp:
            clicked = False
            for text in DMAMA_TEXTS:
                if not clicked:
                    try:
                        await page.get_by_role("button", name=re.compile(text)).click(timeout=5000)
                        clicked = True
                        break
                    except Exception:
                        try:
                            await page.get_by_role("link", name=re.compile(text)).click(timeout=5000)
                            clicked = True
                            break
                        except Exception:
                            continue
            if not clicked:
                # fallback: หา element ที่มีคำว่า Dmama
                await page.locator(':is(button,a):has-text("Dmama")').first.click(timeout=5000)
        dmama = await newp.value
    except PWTimeout:
        dmama = page  # อยู่แท็บเดิม

    # ไปหน้า Analysis โดยตรง
    try:
        await dmama.goto(DMAMA_ANAL, wait_until="domcontentloaded")
    except Exception:
        pass
    await dmama.wait_for_timeout(800)

    # เปิด modal “รายการมาตรวัด”
    opened = False
    for sel in [
        'input[placeholder*="รายการมาตรวัด"]',
        'div:has-text("รายการมาตรวัด") input',
        'text=รายการมาตรวัด'
    ]:
        try:
            await dmama.click(sel, timeout=4000)
            opened = True
            break
        except Exception:
            continue

    if opened:
        # ติ๊ก "เลือกทั้งหมด"
        try:
            await dmama.get_by_label(SELECT_ALL, exact=False).check(timeout=4000)
        except Exception:
            try:
                await dmama.locator(f'label:has-text("{SELECT_ALL}")').click(timeout=4000)
            except Exception:
                pass
        await dmama.wait_for_timeout(300)

        # กด "ตกลง" (เอาปุ่มท้ายสุดกันกรณีมีหลาย modal)
        try:
            okbtns = dmama.get_by_role("button", name=re.compile(OK_TEXT))
            count = await okbtns.count()
            if count > 0:
                await okbtns.nth(count - 1).click()
            else:
                await dmama.locator(f'button:has-text("{OK_TEXT}")').last.click()
        except Exception:
            pass

    # (ถ้ามี) ปุ่มค้นหา
    try:
        await dmama.get_by_role("button", name=re.compile(SEARCH_TEXT)).click(timeout=2500)
    except Exception:
        pass

    return dmama


async def _verify_green_status(page):
    """เปิด app.html, reload 1 ครั้ง และตรวจข้อความสีเขียว 2 ประโยคตามตัวอย่าง"""
    await page.goto(WTMS_APP, wait_until="domcontentloaded")
    await page.wait_for_timeout(800)
    await page.reload(wait_until="domcontentloaded")
    html = await page.content()
    ok = ("รับทราบข้อมูล WTMS เมื่อ" in html) and ("มีการใช้งานระบบ DMAMA ครบตามเงื่อนไขแล้ว" in html)
    return ok


# ---------- Exported flow (ใช้ทั้ง webhook และ runner.py) ----------
async def run_wtms_flow() -> dict:
    """
    ทำงานครบ:
      - Login WTMS
      - กด 'รับทราบข้อมูล' (ถ้ามี)
      - เข้าระบบ DMAMA → Analysis → เลือกทั้งหมด → ตกลง → (ค้นหา)
      - เปิด WTMS app.html → reload → ตรวจว่าขึ้นสีเขียวครบ
    คืนค่า:
      { "ok": bool, "shots": [url,...], "error": str? }
    """
    shots = []
    try:
        async with async_playwright() as p:
            # หมายเหตุ: ถ้ารันด้วย root บน VPS ใช้ --no-sandbox
            browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
            context = await browser.new_context(viewport={"width": 1366, "height": 900})
            page = await context.new_page()
            page.set_default_timeout(TIMEOUT_MS)

            # 1) Login
            await _login_wtms(page)
            shots.append(await _snap(page, "01_login_ok"))

            # 2) รับทราบ (optional)
            try:
                acked = await _click_optional_ack(page)
                if acked:
                    await page.wait_for_timeout(400)
            except Exception:
                pass

            # 3) Dmama → Analysis → Select All
            dmama = await _open_dmama_and_select_all(context, page)
            shots.append(await _snap(dmama, "02_dmama_analysis"))

            # 4) Verify green status on WTMS app.html
            ok = await _verify_green_status(page)
            shots.append(await _snap(page, "03_wtms_status"))

            await context.close()
            await browser.close()
            return {"ok": ok, "shots": shots}

    except Exception as e:
        return {"ok": False, "shots": shots, "error": str(e)}


# ---------- FastAPI endpoints ----------
@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/callback", response_class=PlainTextResponse)
async def callback(request: Request):
    """LINE webhook endpoint"""
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
                        TextSendMessage(text="🕐 รับคำสั่งแล้ว กำลังตรวจ WTMS/DMAMA…")
                    )
                except Exception:
                    pass

                # ทำงาน async แล้ว push กลับ
                async def worker(user_id: str):
                    result = await run_wtms_flow()
                    ok = bool(result.get("ok"))
                    shots = result.get("shots", [])
                    status_txt = "✅ ครบเงื่อนไข" if ok else "⚠️ ยังไม่ครบเงื่อนไข"
                    msgs = [TextSendMessage(text=f"ผลตรวจ WTMS/DMAMA: {status_txt}")]
                    for url in shots:
                        msgs.append(ImageSendMessage(original_content_url=url, preview_image_url=url))
                    try:
                        line_bot_api.push_message(user_id, msgs)
                    except Exception as e:
                        try:
                            line_bot_api.push_message(user_id, TextSendMessage(text=f"ผิดพลาด: {e}"))
                        except Exception:
                            pass

                # fire-and-forget (ไม่บล็อก webhook)
                asyncio.create_task(worker(ev.source.user_id))

    return "OK"
