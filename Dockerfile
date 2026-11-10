FROM python:3.13-slim-bookworm AS dependencies

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

FROM python:3.13-slim-bookworm

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --system panel \
    && useradd --system --gid panel --no-create-home panel \
    && install -d -m 0700 -o panel -g panel /app/sessions

WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv
COPY . .

USER panel
EXPOSE 8765
CMD ["python", "web_panel.py", "--listen", "0.0.0.0"]
