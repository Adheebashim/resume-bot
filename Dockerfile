FROM python:3.12-slim

# LibreOffice converts the tailored .docx to PDF. Carlito is metric-compatible with
# Calibri (used by all three templates), so the layout stays identical to Word.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libreoffice-writer \
       fonts-crosextra-carlito \
       fonts-liberation \
       fonts-dejavu-core \
       fontconfig \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
