FROM python:3.11-slim-bookworm

WORKDIR /app

COPY requirements.txt .

# Step 1: 装 Momenta 内部包（pyevents 锁 pydantic v1，先让它装）
RUN pip install --no-cache-dir \
    --index-url https://artifactory.momenta.works/artifactory/api/pypi/pypi-momenta/simple \
    --extra-index-url https://artifactory.momenta.works/artifactory/api/pypi/pypi-pl/simple \
    --extra-index-url https://pypi.org/simple/ \
    --trusted-host artifactory.momenta.works \
    pyevents>=1.2 pyfeishu>=0.1.0 datetime

# Step 2: 强制升级 pydantic 到 v2（pyevents 实际兼容 v2，只是 setup.py 写死了）
RUN pip install --no-cache-dir --force-reinstall \
    'pydantic>=2.5.0'

# Step 3: 装其余依赖（fastapi 等）
RUN pip install --no-cache-dir \
    --index-url https://artifactory.momenta.works/artifactory/api/pypi/pypi-remote/simple \
    --extra-index-url https://artifactory.momenta.works/artifactory/api/pypi/pypi-momenta/simple \
    --trusted-host artifactory.momenta.works \
    fastapi>=0.109.0 'uvicorn[standard]>=0.27.0' 'pydantic-settings>=2.1.0' \
    python-dotenv requests 'httpx>=0.26.0' feishu-sync

# Step 4: 安装 msgpack（DPI 数据解析用）
RUN pip install --no-cache-dir msgpack

COPY . .

EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
