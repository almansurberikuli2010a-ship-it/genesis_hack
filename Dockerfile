# Официальная проверка (без сети):
#   docker build -t auditgym .
#   docker run --rm --network none --cpus 2 --memory 4g auditgym
FROM python:3.11.11-slim-bookworm
# UTF-8 явно: вывод, отчёты и данные содержат кириллицу
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 LANG=C.UTF-8 LC_ALL=C.UTF-8
WORKDIR /app
COPY . /app
RUN useradd --create-home --uid 10001 auditor && chown -R auditor /app
USER auditor
CMD ["python", "-m", "auditgym", "check"]
