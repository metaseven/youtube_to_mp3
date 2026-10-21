FROM ghcr.io/metaseven/youtube_to_mp3:latest

WORKDIR /app
COPY --chown=botuser:botuser . /app

