# ใช้ image ที่มี Chromium ของ Playwright ติดตั้งมาแล้ว
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

# ตั้ง working directory ภายใน container
WORKDIR /app

# คัดลอกไฟล์ทั้งหมดในโปรเจกต์ของคุณลง container
COPY . /app

# ติดตั้ง Python package ที่จำเป็น
RUN pip install --no-cache-dir -r requirements.txt

# ติดตั้ง Chromium (เผื่อ image ไม่มีครบ)
RUN python -m playwright install chromium

# ตั้งค่า port ที่จะใช้ (Render หรือ Uvicorn จะอ่านจาก ENV นี้)
ENV PORT=8000

# คำสั่งหลักเมื่อ container เริ่มทำงาน
CMD ["bash", "-lc", "uvicorn app:app --host 0.0.0.0 --port $PORT"]
