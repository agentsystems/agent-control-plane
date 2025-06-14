FROM python:3.12-slim
RUN pip install fastapi httpx uvicorn[standard] docker
WORKDIR /app
COPY main.py /app
USER 1001
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

