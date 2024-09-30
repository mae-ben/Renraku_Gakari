FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get update && apt-get install -y libffi-dev

EXPOSE 8000

COPY . .

CMD ["python", "Renraku_Gakari.py"]
