FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY data/ ./data/

# HF Spaces requires the app to listen on 0.0.0.0:7860
ENV PORT=7860
EXPOSE 7860

CMD ["python", "-u", "app.py"]
