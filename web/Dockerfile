FROM python:3.12-slim

WORKDIR /app

# server.py imports cli/agent_client.py directly (sys.path insert at
# startup) — both directories must be present in the image. Only cli/
# agent_client.py is actually used at runtime, but the whole cli/ dir is
# copied for simplicity; it's a handful of small files.
COPY web/requirements.txt ./web/requirements.txt
RUN pip install --no-cache-dir -r web/requirements.txt

COPY web/ ./web/
COPY cli/agent_client.py ./cli/agent_client.py

# No secrets or credentials baked into the image — AWS credentials come
# from the ECS task's IAM role at runtime (the default boto3 credential
# chain finds them via the container's task metadata endpoint).
EXPOSE 8420

ENTRYPOINT ["python", "web/server.py"]
