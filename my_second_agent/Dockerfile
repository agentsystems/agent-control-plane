FROM python:3.12-slim

COPY requirements.txt /tmp/
RUN pip install --upgrade pip \
 && pip install --no-cache-dir -r /tmp/requirements.txt   # ← succeeds now

WORKDIR /app
COPY . /app

RUN useradd -u 1001 appuser
USER 1001

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

