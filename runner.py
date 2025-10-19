# runner.py
import os, asyncio
from dotenv import load_dotenv
from linebot import LineBotApi
from linebot.models import TextSendMessage, ImageSendMessage

# เราใช้ run_wtms_flow() เดิมจาก app.py เพื่อไม่ต้องเขียนซ้ำ
from app import run_wtms_flow

load_dotenv()
LINE_TOKEN   = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")  # ใส่ userId ของคุณ/กลุ่มที่ให้ส่งผล

async def main():
    result = await run_wtms_flow()
    api = LineBotApi(LINE_TOKEN)
    status_txt = "✅ ครบเงื่อนไข" if result.get("ok") else "⚠️ ยังไม่ครบเงื่อนไข"
    msgs = [TextSendMessage(text=f"[WTMS/DMAMA] {status_txt}")]
    for url in result.get("shots", []):
        msgs.append(ImageSendMessage(original_content_url=url, preview_image_url=url))
    api.push_message(LINE_USER_ID, msgs)

if __name__ == "__main__":
    asyncio.run(main())
