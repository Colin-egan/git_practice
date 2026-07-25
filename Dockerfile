FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY darwin/ darwin/
VOLUME /data
ENV DARWIN_DB=/data/darwin.db
EXPOSE 8000
CMD ["uvicorn", "darwin.server:app", "--host", "0.0.0.0", "--port", "8000"]
