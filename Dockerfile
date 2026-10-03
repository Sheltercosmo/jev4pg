FROM python:3.13-slim-bookworm AS build
WORKDIR /build
COPY requirements.lock.txt pyproject.toml LICENSE NOTICE ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY sdd ./sdd
RUN pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SDD_ENV=production
WORKDIR /app
COPY requirements.lock.txt /tmp/requirements.lock.txt
RUN pip install --no-cache-dir -r /tmp/requirements.lock.txt \
    && groupadd --gid 10001 jev \
    && useradd --uid 10001 --gid jev --no-create-home jev
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-deps /wheels/*.whl && rm -r /wheels
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["jevsd-pg"]
CMD ["serve", "--host", "0.0.0.0"]
