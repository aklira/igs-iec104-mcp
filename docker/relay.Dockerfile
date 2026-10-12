# The relay of a test bench (IMPLEMENTATION.md §6; task D2). Build it with the docker directory as the
# context:
#   docker build --file docker/relay.Dockerfile docker
# The base is pinned by its digest, like the runtime image. The relay runs as a non-root user with a fixed
# identity (10002). The same image runs a fault proxy (faultproxy.py) with its own entrypoint; the proxy
# reads the bench token from the run directory, which the image creates for that user (V3b).
FROM docker.io/library/python@sha256:2b4f19dae3a777dfc3b76730bda1e82e1f66ab2a2686fa93ca78edbfb4f04ffe
COPY relay.py /usr/local/bin/relay.py
COPY faultproxy.py /usr/local/bin/faultproxy.py
RUN install -d -o 10002 -g 10002 -m 0700 /run/igs
USER 10002:10002
ENTRYPOINT ["python", "-I", "/usr/local/bin/relay.py"]
