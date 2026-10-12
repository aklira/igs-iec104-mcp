# The independent peer of the interoperability tests (IMPLEMENTATION.md §13; task X3). It runs the
# OpenMUC j60870 library (GNU GPL v3, https://www.openmuc.org/j60870/) as a black box: a station that
# answers an interrogation for one point (Station.java). The library is fetched from Maven Central at a
# pinned version, and its SHA-256 is checked against the value Maven Central publishes. This project
# does not read, copy, or redistribute the library's sources; the image is built locally.
#
#   podman build --file docker/independent/Dockerfile docker/independent
#
# Both images are pinned by digest. The peer runs as a non-root user with a fixed identity (10003).

ARG J60870_VERSION=1.8.0
ARG J60870_SHA256=fd6feff8672e07dfe46253bbcbdc79c842a60b3bbade884d385b701fafe894e7

FROM docker.io/library/eclipse-temurin@sha256:3e3c176ffed168beb42c607be9bc1639b466cf00261a0fb04425562c9d0c5c2b AS build
ARG J60870_VERSION
ARG J60870_SHA256
ADD https://repo1.maven.org/maven2/org/openmuc/j60870/${J60870_VERSION}/j60870-${J60870_VERSION}.jar /opt/peer/j60870.jar
RUN echo "${J60870_SHA256}  /opt/peer/j60870.jar" | sha256sum -c - \
    && chmod 0644 /opt/peer/j60870.jar
COPY Station.java /src/Station.java
RUN javac -cp /opt/peer/j60870.jar -d /opt/peer/classes /src/Station.java

FROM docker.io/library/eclipse-temurin@sha256:80b6aebe2f990697ada73fdbe451c7e9ec5e4adbde7a1e3625bc665e3b2fc06e
RUN groupadd --system --gid 10003 peer \
    && useradd --system --uid 10003 --gid peer --no-create-home --shell /usr/sbin/nologin peer
COPY --from=build /opt/peer /opt/peer
USER 10003:10003
ENTRYPOINT ["java", "-cp", "/opt/peer/classes:/opt/peer/j60870.jar", "Station"]
