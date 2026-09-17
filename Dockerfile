FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py app_v2.py app_v3.py app_v4.py app_v5.py app_v6.py app_v7.py app_v8.py app_v9.py app_v10.py app_v11.py app_v12.py generate_session.py aliases.example.json verified_aliases.json ./

RUN mkdir -p /app/data

CMD ["python", "app_v12.py"]
