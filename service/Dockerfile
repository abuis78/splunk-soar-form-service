# Form service: single-use magic-link forms with file upload, hand-off to Splunk SOAR.
# Standard library only, no pip install.
#   docker build -t soar-form service/
#   docker run -p 8080:8080 -e FORM_BASE_URL=https://forms.example.com -e FORM_API_KEY=... \
#     -e SOAR_URL=https://soar.example.com -e SOAR_TOKEN=... -v soar-form-state:/data soar-form
FROM python:3.13-slim
RUN useradd --system --uid 10001 --home-dir /app formsvc
WORKDIR /app
COPY soar_form/ soar_form/
COPY forms/ forms/
ENV FORM_HOST=0.0.0.0 \
    FORM_PORT=8080 \
    FORM_DIR=/app/forms \
    FORM_STATE_DIR=/data \
    PYTHONUNBUFFERED=1
RUN mkdir /data && chown formsvc /data
USER formsvc
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["python3", "-m", "soar_form"]
