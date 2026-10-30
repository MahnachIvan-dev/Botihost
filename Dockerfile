FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies in a separate layer for reliable caching.
COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir --prefer-binary -r requirements.txt

# Both bots must be present because cashier.py is imported by bot.py.
COPY bot.py ./bot.py
COPY cashier.py ./cashier.py
COPY sitecustomize.py ./sitecustomize.py

RUN mkdir -p /app/data /app/data/bots \
    && python -m py_compile /app/sitecustomize.py /app/bot.py /app/cashier.py

CMD ["python", "-u", "bot.py"]
