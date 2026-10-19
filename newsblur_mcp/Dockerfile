FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --create-home --uid 10001 app
WORKDIR /app
COPY nbmcp ./nbmcp
USER app
CMD ["python", "-m", "nbmcp"]
