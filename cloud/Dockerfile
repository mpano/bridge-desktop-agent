# Bridge Cloud. Build from the repository root (it shares the price list with the app):
#   fly deploy --config cloud/fly.toml --dockerfile cloud/Dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY cloud/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home bridge
COPY app/__init__.py app/__init__.py
COPY app/llm/__init__.py app/llm/__init__.py
COPY app/llm/usage.py app/llm/usage.py
COPY cloud/bridge_cloud bridge_cloud
COPY cloud/start.sh start.sh

EXPOSE 8080
CMD ["sh", "start.sh"]
