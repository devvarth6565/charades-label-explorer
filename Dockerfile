# Minimal image with nothing but Python 3 + bash, i.e. exactly what the brief
# assumes on a "clean Unix machine". Works on Cloud Run, Fly.io, Railway, Render.
#
# The dataset is fetched at build time and baked into the image, so push it
# only to a PRIVATE registry: the Charades license forbids redistributing data.
FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN ./setup.sh --videos 16 && ./run.sh test

ENV HOST=0.0.0.0 \
    PORT=8080
EXPOSE 8080
HEALTHCHECK CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ['PORT'])"
CMD ["./run.sh", "serve", "--quiet"]
