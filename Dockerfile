# Вариант для своего VPS. Версия образа должна совпадать с playwright в requirements-browser.txt
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
COPY requirements.txt requirements-browser.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-browser.txt
COPY . .
CMD ["python", "-m", "jobwatch"]
