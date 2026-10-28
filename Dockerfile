# ClipKaro — Render.com par Docker se deploy (ffmpeg ke sath)
FROM python:3.12-slim

# ffmpeg zaroori hai — video ka merge/convert isi se hota hai.
# Render ke normal Python environment mein ffmpeg nahi hota,
# is liye Docker image mein khud install kar rahe hain.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Render PORT env var deta hai; app.py usay khud parh leti hai
EXPOSE 10000
CMD ["python", "app.py"]
