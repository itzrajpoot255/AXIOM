# AXIOM — container build for free, card-free hosting (Hugging Face Spaces)
FROM python:3.11-slim

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir -r requirements-cloud.txt

# Hugging Face Spaces routes traffic to port 7860 by default.
EXPOSE 7860
ENV PORT=7860

CMD ["python", "axiom.py", "--server", "--host", "0.0.0.0", "--no-browser"]
