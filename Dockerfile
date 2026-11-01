FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worldbrief ./worldbrief
COPY sources.yaml sections.yaml paywall.yaml entrypoint.sh ./

RUN useradd --system --no-create-home app
EXPOSE 8080
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
