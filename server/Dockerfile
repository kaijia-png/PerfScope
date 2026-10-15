FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pymysql boto3
COPY perfserver ./perfserver
COPY plugins ./plugins
ENV DATABASE_URL=sqlite:////data/perfscope.db BLOB_DIR=/data/blobs
VOLUME /data
EXPOSE 9000
CMD ["uvicorn", "perfserver.main:app", "--host", "0.0.0.0", "--port", "9000"]
