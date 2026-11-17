# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 DATA_DIR=/app/data
WORKDIR /app
RUN groupadd --gid 10001 circuit && useradd --uid 10001 --gid circuit --no-create-home circuit
COPY requirements.lock /app/requirements.lock
COPY build-requirements.lock /app/build-requirements.lock
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    pip install --no-cache-dir -r requirements.lock -r build-requirements.lock
COPY pyproject.toml /app/pyproject.toml
COPY --chown=circuit:circuit circuit_bot /app/circuit_bot
RUN pip install --no-cache-dir --no-deps --no-build-isolation . && mkdir /app/data && chown circuit:circuit /app/data && chmod 700 /app/data
USER circuit
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD ["circuit-bot", "healthcheck"]
CMD ["circuit-bot", "run"]
