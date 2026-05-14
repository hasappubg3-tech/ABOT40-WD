FROM python:3.11-slim

WORKDIR /app

# نسخ ملف المتطلبات أولاً لتحسين سرعة البناء
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ بقية الملفات بما فيها قاعدة البيانات
COPY . .

CMD ["python", "main.py"]
