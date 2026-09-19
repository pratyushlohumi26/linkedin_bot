FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple -r requirements.txt
COPY --chmod=0644 app.py egress_proxy.py /app/browser_service/
RUN chmod 0755 /app/browser_service
USER pwuser
CMD ["python", "-m", "browser_service.app"]
