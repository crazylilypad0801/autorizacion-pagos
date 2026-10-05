FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
COPY server.py index.html ./
RUN mkdir -p /data
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1
CMD ["python", "server.py"]
