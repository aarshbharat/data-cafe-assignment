FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY data/ ./data/
COPY eval/ ./eval/

# Fail the build rather than the first request if the pack is unreadable.
RUN python -m src.prepare

ENV PORT=8000
EXPOSE 8000
CMD ["python", "-m", "src.solution"]
