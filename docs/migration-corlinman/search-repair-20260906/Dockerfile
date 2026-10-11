# Overlay the exact running image; preserve its existing dependencies and OS.
ARG BASE_IMAGE=sha256:f614580b9ca33379c09f68dc6b23578f1a05df411ce02d2b3e3ae8c30280418b
FROM ${BASE_IMAGE}
USER root
WORKDIR /tmp
COPY baseline-constraints.txt requirements.search.txt /tmp/search-repair/
COPY wheelhouse/ /tmp/search-repair/wheelhouse/
RUN python -m pip install --only-binary=:all: --no-cache-dir \
    --no-index --find-links=/tmp/search-repair/wheelhouse \
    -c /tmp/search-repair/baseline-constraints.txt \
    -r /tmp/search-repair/requirements.search.txt \
    --report /opt/hermes/search-dependencies.json
USER 991:988
WORKDIR /opt/hermes/repo
