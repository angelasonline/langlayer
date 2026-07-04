FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY langlayer ./langlayer
EXPOSE 8000
# OPENAI_API_KEY (optional): enables live model providers
CMD ["uvicorn", "langlayer.api:app", "--host", "0.0.0.0", "--port", "8000"]
