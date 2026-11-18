FROM python:3.13-slim
WORKDIR /app
COPY requirements.lock pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir -e . --no-deps
ENV SPORTS_ARB_DATA=/data
VOLUME /data
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["sports-arb-service", "check"]
CMD ["sports-arb-service", "run"]
