FROM python:3.12-slim
WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
EXPOSE 8501
CMD ["streamlit", "run", "app/app.py", \
     "--server.port=8501", "--server.address=0.0.0.0", \
     "--server.maxUploadSize=2000", "--server.fileWatcherType=poll", \
     "--browser.gatherUsageStats=false"]