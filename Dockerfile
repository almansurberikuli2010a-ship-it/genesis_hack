# Официальная проверка (без сети):
#   docker build -t auditgym .
#   docker run --rm --network none --cpus 2 --memory 4g auditgym
# Коммит в reports/check_report.json → git.commit (.git в образ не копируется):
#   docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) -t auditgym .
FROM python:3.11.11-slim-bookworm
# UTF-8 явно: вывод, отчёты и данные содержат кириллицу
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 LANG=C.UTF-8 LC_ALL=C.UTF-8
# без пользовательского site-packages: политика не подложит код (usercustomize, .pth) следующим процессам
ENV PYTHONNOUSERSITE=1
# без аргумента — пустая строка, и check пишет commit = null
ARG GIT_COMMIT=
ENV AUDITGYM_GIT_COMMIT=$GIT_COMMIT
WORKDIR /app
COPY . /app
# код среды и оценщика остаётся за root: пользователь прогона (и его политика) не может его изменить;
# писать он может только отчёты в reports/
RUN useradd --create-home --uid 10001 auditor && mkdir -p /app/reports && chown -R auditor /app/reports
USER auditor
CMD ["python", "-m", "auditgym", "check"]
