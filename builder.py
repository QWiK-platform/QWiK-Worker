import os
import json
import subprocess
import sys
import mimetypes
import boto3
import psycopg2
from urllib.parse import urlparse
from botocore.exceptions import NoCredentialsError, ClientError


# EventBridge Pipes override로 전달되는 환경변수
REPO_URL = os.environ.get('REPO_URL')
USER_ID = os.environ.get('USER_ID')
USERNAME = os.environ.get('USERNAME')
PROJECT_ID = os.environ.get('PROJECT_ID')
DEPLOYMENT_ID = os.environ.get('DEPLOYMENT_ID')

# Task Definition에서 주입되는 환경변수
S3_BUCKET_NAME = os.environ.get('S3_BUCKET_NAME')
DATABASE_URL = os.environ.get('DATABASE_URL')
KVS_ARN = os.environ.get('KVS_ARN')
DISTRIBUTION_ID = os.environ.get('DISTRIBUTION_ID')

PROJECT_ROOT = "/app/source"

s3 = boto3.client('s3')
kvs_client = boto3.client('cloudfront-keyvaluestore')
cf_client = boto3.client('cloudfront')


# -- KVS 로직
def get_kvs_etag():
    response = kvs_client.describe_key_value_store(KvsARN=KVS_ARN)
    return response['ETag']


def generate_subdomain():
    return f"{USERNAME}-{PROJECT_ID[:7]}"


def update_kvs_mapping(subdomain: str, s3_path_prefix: str):
    """KVS에 서브도메인 -> S3 경로 매핑 추가"""
    print(f"[KVS] Updating mapping: {subdomain} -> {s3_path_prefix}")

    try:
        etag = get_kvs_etag()
        kvs_client.put_key(
            KvsARN=KVS_ARN,
            Key=subdomain,
            Value=s3_path_prefix,
            IfMatch=etag
        )
        print("[KVS] Mapping created")
    except ClientError as e:
        print(f"[ERROR] [KVS] {e}")
        raise


def invalidate_cache(s3_path: str):
    """CloudFront 캐시 무효화 (특정 경로만)"""
    invalidation_path = f"/{s3_path}/*"

    try:
        cf_client.create_invalidation(
            DistributionId=DISTRIBUTION_ID,
            InvalidationBatch={
                'Paths': {
                    'Quantity': 1,
                    'Items': [invalidation_path]
                },
                'CallerReference': f"{PROJECT_ID}-{int(__import__('time').time())}"
            }
        )
        print("[CDN] Cache invalidation requested")
    except ClientError as e:
        print(f"[ERROR] [CDN] {e}")
        raise


# -- DB 로직
def get_db_connection():
    """DATABASE_URL 파싱 후 PostgreSQL 연결 생성"""
    parsed = urlparse(DATABASE_URL)
    return psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path[1:],  # /dbname -> dbname
        user=parsed.username,
        password=parsed.password
    )


def get_existing_domain():
    """프로젝트에 이미 도메인이 있는지 확인 (재배포 여부 판단)"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT domain
                FROM projects
                WHERE project_id = %s
            """, (PROJECT_ID,))
            result = cur.fetchone()
            return result[0] if result and result[0] else None
    finally:
        conn.close()

def update_deployment_status(status: str, subdomain: str = None, s3_path: str = None):
    """Deployment 상태 업데이트 (BUILDING, SUCCESS, FAILED)"""

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Deployment 상태 업데이트
            cur.execute(
                """
                UPDATE deployments
                SET status = %s
                WHERE deployment_id = %s
                """,
                (status, DEPLOYMENT_ID)
            )

            # 첫 배포: 도메인과 s3_path 업데이트
            if subdomain and s3_path:
                cur.execute(
                    """
                    UPDATE projects
                    SET status = TRUE, domain = %s, s3_path = %s
                    WHERE project_id = %s
                    """,
                    (subdomain, s3_path, PROJECT_ID)
                )

            # 재배포 성공 시: projects.status = TRUE (subdomain/s3_path 없이 SUCCESS인 경우)
            if status == 'SUCCESS' and not subdomain and not s3_path:
                cur.execute(
                    """
                    UPDATE projects
                    SET status = TRUE
                    WHERE project_id = %s
                    """,
                    (PROJECT_ID,)
                )

            # 실패 시: projects.status = FALSE
            if status == 'FAILED':
                cur.execute(
                    """
                    UPDATE projects
                    SET status = FALSE
                    WHERE project_id = %s
                    """,
                    (PROJECT_ID,)
                )
        conn.commit()
        print(f"[DB] Status updated: {status}")
    except Exception as e:
        print(f"[ERROR] [DB] Failed to update status: {e}")
        raise
    finally:
        conn.close()


def get_directory_size(path: str) -> int:
    """디렉토리 총 용량 계산 (bytes)"""
    total = 0
    for root, dirs, files in os.walk(path):
        for file in files:
            total += os.path.getsize(os.path.join(root, file))
    return total


def update_storage_usage(size_bytes: int):
    """프로젝트 스토리지 사용량 업데이트"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE usage
                SET storage_used = %s
                WHERE project_id = %s
            """, (size_bytes, PROJECT_ID))
        conn.commit()
        print(f"[DB] Storage usage: {size_bytes} bytes ({size_bytes / 1024 / 1024:.2f} MB)")
    except Exception as e:
        print(f"[ERROR] [DB] Failed to update storage usage: {e}")
        raise
    finally:
        conn.close()


def run_command(command, cwd=None):
    print(f"Executing: {' '.join(command)}")
    try:
        subprocess.run(
            command,
            cwd=cwd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True
        )
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Command failed (exit code {e.returncode})")
        print(f"[ERROR] {e.stderr}")
        raise e


# -- 빌드 로직
# git clone 하는 함수
def clone_repo():
    if os.path.exists(PROJECT_ROOT):
        import shutil
        shutil.rmtree(PROJECT_ROOT)

    run_command(["git", "clone", REPO_URL, PROJECT_ROOT])

# 정적 사이트 여부 확인 (package.json 없으면 정적 사이트)
def is_static_site():
    return not os.path.exists(os.path.join(PROJECT_ROOT, 'package.json'))

# npm, yarn, pnpm 중 하나를 감지하고 의존성 설치 및 빌드 수행하는 함수
def install_dependencies_and_build():
    cwd = PROJECT_ROOT

    # Lock 파일을 기반으로 탐지
    if os.path.exists(os.path.join(cwd, 'yarn.lock')):
        print("[BUILD] Detected: yarn")
        run_command(["yarn", "install", "--frozen-lockfile"], cwd=cwd)
        run_command(["yarn", "build"], cwd=cwd)

    elif os.path.exists(os.path.join(cwd, 'pnpm-lock.yaml')):
        print("[BUILD] Detected: pnpm")
        run_command(["pnpm", "install", "--frozen-lockfile"], cwd=cwd)
        run_command(["pnpm", "build"], cwd=cwd)

    else:
        # 기본값: npm
        print("[BUILD] Detected: npm")
        run_command(["npm", "install"], cwd=cwd)
        run_command(["npm", "run", "build"], cwd=cwd)

# 빌드 산출물 디렉토리 찾는 함수
def find_build_output():
    candidates = ['dist', 'build', '.next/server/pages']  # Next.js의 경우 추가 설정 필요할 수 있음

    for folder in candidates:
        path = os.path.join(PROJECT_ROOT, folder)
        if os.path.exists(path) and os.path.isdir(path):
            print(f"[BUILD] Output directory: {path}")
            return path

    raise FileNotFoundError("Could not find build output directory (dist/build).")

def clear_s3_path(s3_path_prefix: str):
    """S3 경로 내 기존 파일 삭제"""
    print(f"[S3] Clearing existing files: {s3_path_prefix}")

    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=s3_path_prefix):
        if 'Contents' in page:
            objects = [{'Key': obj['Key']} for obj in page['Contents']]
            s3.delete_objects(Bucket=S3_BUCKET_NAME, Delete={'Objects': objects})
            print(f"[S3] Deleted {len(objects)} objects")


# s3에 업로드하는 함수
def upload_to_s3(local_path):
    # 기존 파일 삭제
    s3_path_prefix = f"users/{USER_ID}/{PROJECT_ID}"
    clear_s3_path(s3_path_prefix)

    file_count = 0
    for root, _, files in os.walk(local_path):
        for file in files:
            local_file_path = os.path.join(root, file)
            relative_path = os.path.relpath(local_file_path, local_path)

            # 이게 우리 DB에 들어갈 s3_path
            s3_key = f"users/{USER_ID}/{PROJECT_ID}/{relative_path}"

            content_type, _ = mimetypes.guess_type(local_file_path)
            if content_type is None:
                content_type = 'application/octet-stream'

            try:
                s3.upload_file(
                    local_file_path,
                    S3_BUCKET_NAME,
                    s3_key,
                    ExtraArgs={'ContentType': content_type}
                )
                file_count += 1
            except NoCredentialsError:
                print("[ERROR] [S3] AWS credentials not found")
                raise

    print(f"[S3] Uploaded {file_count} files")
    return file_count


def main():
    try:
        print("======== START DEPLOYMENT ========")
        print(f"[INFO] Project: {PROJECT_ID}")
        print(f"[INFO] User: {USERNAME}")
        print(f"[INFO] Repo: {REPO_URL}")
        update_deployment_status('BUILDING')

        print("======== CLONE REPOSITORY ========")
        clone_repo()

        print("======== INSTALL & BUILD ========")
        if is_static_site():
            print("[BUILD] Static site detected (no package.json)")
            print("[BUILD] Skipping install & build")
            build_output_path = PROJECT_ROOT
        else:
            install_dependencies_and_build()
            build_output_path = find_build_output()

        # 빌드 결과물 용량 계산 및 저장
        build_size = get_directory_size(build_output_path)
        update_storage_usage(build_size)

        print("======== UPLOAD TO S3 ========")
        file_count = upload_to_s3(build_output_path)

        print("======== UPDATE ROUTING ========")
        existing_domain = get_existing_domain()

        if existing_domain:
            # 재배포: KVS/DB 업데이트 스킵, status만 업데이트
            print(f"[ROUTING] Redeploy detected: {existing_domain}")
            s3_path = f"users/{USER_ID}/{PROJECT_ID}"
            invalidate_cache(s3_path)
            update_deployment_status('SUCCESS')
            deploy_url = f"https://{existing_domain}.qw1k.cloud"
        else:
            # 첫 배포: 임시 subdomain 생성 + KVS/DB 업데이트
            subdomain = generate_subdomain()
            s3_path = f"users/{USER_ID}/{PROJECT_ID}"
            update_kvs_mapping(subdomain, f"/{s3_path}")
            update_deployment_status('SUCCESS', subdomain, s3_path)
            deploy_url = f"https://{subdomain}.qw1k.cloud"

        print("======== DEPLOYMENT COMPLETE ========")
        print(f"[RESULT] {deploy_url}")
        print(f"[SUMMARY] Files: {file_count} | Size: {build_size / 1024 / 1024:.2f} MB")

    except Exception as e:
        print("======== DEPLOYMENT FAILED ========")
        print(f"[ERROR] {e}")
        try:
            update_deployment_status('FAILED')
        except:
            print("[ERROR] [DB] Failed to update status to FAILED")
        sys.exit(1)

if __name__ == "__main__":
    main()