FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY autogatus/ ./autogatus/

# Writes the compiled Gatus endpoint file here; mount this into the Gatus
# config directory (or a shared volume Gatus reads) so Gatus hot-reloads it.
ENV AUTOGATUS_OUTPUT=/output/autogatus.yaml

ENTRYPOINT ["python", "-m", "autogatus"]
