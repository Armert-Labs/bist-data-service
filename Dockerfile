# --- Build asamasi: bagimliliklari ve paketi derle ---
# 3.13: CI test matrisiyle ayni surum (3.14 hicbir testten gecmiyordu).
FROM python:3.14-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src
# Bagimlilik katmani app kodundan once: app/*.py degisikligi 56 pinli paketin
# yeniden kurulumunu tetiklemesin.
COPY pyproject.toml README.md requirements.lock ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.lock
COPY app ./app
RUN pip install --no-cache-dir --prefix=/install --no-deps .

# --- Runtime asamasi: yalin, root olmayan ---
FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Healthcheck icin curl + root olmayan kullanici
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

COPY --from=builder /install /usr/local

# Lock/pyproject uyumsuzlugunu (eksik/celisen bagimlilik) build aninda yakala.
RUN pip check

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
