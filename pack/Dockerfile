FROM python:3.12-slim

WORKDIR /srv/pack

COPY apps/api/requirements.txt /srv/pack/apps/api/requirements.txt
RUN pip install --no-cache-dir -r /srv/pack/apps/api/requirements.txt

COPY apps/api /srv/pack/apps/api
COPY apps/bot /srv/pack/apps/bot
COPY fixtures /srv/pack/fixtures

ENV PACK_FIXTURES_DIR=/srv/pack/fixtures/missions
ENV PYTHONPATH=/srv/pack/apps/api
ENV CCI_SOURCE=mock
ENV LIVE_CCI=false
ENV PACK_ENV=staging
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/pack/apps/api
EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
