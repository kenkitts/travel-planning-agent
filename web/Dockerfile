FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# No secrets or credentials baked into the image — AWS credentials come
# from the ECS task's IAM role at runtime (the default boto3 credential
# chain finds them via the container's task metadata endpoint).
EXPOSE 8420

ENTRYPOINT ["python", "server.py"]
