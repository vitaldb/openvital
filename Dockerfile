FROM python:3.11-slim

COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

ENV AWS_LWA_PORT=3000
ENV AWS_LWA_READINESS_CHECK_PATH=/

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc gfortran libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    numpy \
    'openecg>=0.11.0' \
    scipy

COPY openvital /app/openvital

EXPOSE 3000

CMD ["python", "-m", "openvital"]
