FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       qemu-utils \
       openssh-client \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md /app/
COPY vmware_to_proxmox /app/vmware_to_proxmox
COPY webui /app/webui
COPY templates /app/templates
COPY static /app/static
COPY manage.py /app/manage.py
COPY config.example.yaml /app/config.example.yaml
COPY entrypoint.sh /app/entrypoint.sh

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir . \
    && chmod +x /app/entrypoint.sh \
    && mkdir -p /app/data /app/staticfiles

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "webui.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
