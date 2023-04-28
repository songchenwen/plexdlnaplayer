FROM python:3.9

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

ENV HTTP_PORT=32488 CONFIG_PATH=/config
EXPOSE 1910/udp 32412/udp $HTTP_PORT
VOLUME $CONFIG_PATH

COPY . .

CMD ["python", "-OO", "main.py"]
