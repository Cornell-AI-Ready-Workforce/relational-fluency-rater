# Build for Fargate with:
#
#     docker build --platform linux/amd64 --build-arg BUILD_SHA=$SHA -t $REPO:$SHA .
#
# Adapted from relational-fluency@f9947d2:Dockerfile; the reasons carry over.
#
# `--build-arg BUILD_SHA=$SHA`, with the same $SHA as the tag, is how the
# running image knows which commit it is (server/build_info.py). Leave it out
# and the image still works, but /health reports build null and the daily drift
# check (.github/workflows/prod-build-drift.yml) fails on it.
#
# `--platform linux/amd64` is not optional on an Apple Silicon Mac. An arm64
# build succeeds silently and pushes, and the task then dies on start with an
# exec format error. ECR tags here are IMMUTABLE and the tag is the short git
# SHA, so the same commit cannot be re-pushed correctly afterwards. The task
# definition also declares runtime_platform X86_64 (infra/terraform/ecs.tf),
# so the two say the same thing.
#
# 3.12 is deliberate and is the version CI treats as production.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code only. There is no data directory: this app keeps nothing on
# local disk in production (step 1 is read from its bucket, this app's records
# go to its own), so the container filesystem can be read-only (ecs.tf).
COPY server ./server
COPY static ./static

# Drop root. Same uid as step 1's image (1000), so the two images behave alike
# under the same operational assumptions.
RUN useradd -m -u 1000 appuser && chown -R appuser /app
USER appuser

# The commit this image is built from, published on /health. Declared after the
# pip and COPY layers, because an ARG invalidates the cache from its first use
# and this value changes on every build.
ARG BUILD_SHA=""
ENV BUILD_SHA=$BUILD_SHA

EXPOSE 8080

CMD ["python", "-m", "server.app"]
