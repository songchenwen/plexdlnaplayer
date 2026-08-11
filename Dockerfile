FROM python:3.12

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Without this, print() output sits in the stdio buffer and never reaches
# `docker logs` for a service that is idle most of the time.
ENV PYTHONUNBUFFERED=1
ENV HTTP_PORT=32488 CONFIG_PATH=/config
EXPOSE 1910/udp 32412/udp $HTTP_PORT
VOLUME $CONFIG_PATH

COPY . .

CMD ["python", "-OO", "main.py"]
