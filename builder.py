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
DEPLOYMENT_ID = os.environ.get('DEPLOYMENT_ID')

# Task Definition에서 주입되는 환경변수
S3_BUCKET_NAME = os.environ.get('S3_BUCKET_NAME')
DATABASE_URL = os.environ.get('DATABASE_URL')
KVS_ARN = os.environ.get('KVS_ARN')

PROJECT_ROOT = "/app/source"

s3 = boto3.client('s3')
kvs_client = boto3.client('cloudfront-keyvaluestore')


# -- KVS 로직
def get_kvs_etag():
    response = kvs_client.describe_key_value_store(KvsARN=KVS_ARN)
    return response['ETag']


def generate_subdomain():
    return f"{USERNAME}-{DEPLOYMENT_ID[:7]}"


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
        print(f"[KVS] Mapping created successfully")
    except ClientError as e:
        print(f"[KVS Error] {e}")
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
                SELECT p.domain
                FROM projects p
                JOIN deployments d ON p.project_id = d.project_id
                WHERE d.deployment_id = %s
            """, (DEPLOYMENT_ID,))
            result = cur.fetchone()
            return result[0] if result and result[0] else None
    finally:
        conn.close()

def update_deployment_status(status: str, subdomain: str = None, s3_path: str = None):
    """Deployment 상태 업데이트 (BUILDING, SUCCESS, FAILED)"""
    print(f"[DB] Updating deployment status: {status}")

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
                    FROM deployments
                    WHERE projects.project_id = deployments.project_id
                    AND deployments.deployment_id = %s
                    """,
                    (subdomain, s3_path, DEPLOYMENT_ID)
                )
        conn.commit()
        print(f"[DB] Status updated to: {status}")
    except Exception as e:
        print(f"[DB Error] Failed to update status: {e}")
        raise
    finally:
        conn.close()

def run_command(command, cwd=None):

    print(f"[Process] Executing: {' '.join(command)}")
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,          # Exit Code가 0이 아니면 CalledProcessError 발생
            text=True,           
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        print(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"[Error] Command failed with exit code {e.returncode}")
        print(f"[Error] Stderr: {e.stderr}")
        raise e


# -- 빌드 로직
# git clone 하는 함수
def clone_repo():
    print("--- Step 1: Cloning Repository ---")
    if os.path.exists(PROJECT_ROOT):

        import shutil
        shutil.rmtree(PROJECT_ROOT)
    
    run_command(["git", "clone", REPO_URL, PROJECT_ROOT])

# 정적 사이트 여부 확인 (package.json 없으면 정적 사이트)
def is_static_site():
    return not os.path.exists(os.path.join(PROJECT_ROOT, 'package.json'))

# npm, yarn, pnpm 중 하나를 감지하고 의존성 설치 및 빌드 수행하는 함수
def install_dependencies_and_build():
    print("--- Step 2 & 3: Detect Manager, Install & Build ---")

    cwd = PROJECT_ROOT

    # Lock 파일을 기반으로 탐지
    if os.path.exists(os.path.join(cwd, 'yarn.lock')):
        print("Detected: Yarn")
        run_command(["yarn", "install", "--frozen-lockfile"], cwd=cwd)
        run_command(["yarn", "build"], cwd=cwd)

    elif os.path.exists(os.path.join(cwd, 'pnpm-lock.yaml')):
        print("Detected: pnpm")
        run_command(["pnpm", "install", "--frozen-lockfile"], cwd=cwd)
        run_command(["pnpm", "build"], cwd=cwd)

    else:
        # 기본값: npm 
        print("Detected: npm")
        run_command(["npm", "install"], cwd=cwd)
        run_command(["npm", "run", "build"], cwd=cwd)

# 빌드 산출물 디렉토리 찾는 함수
def find_build_output():
    print("--- Step 4: Finding Build Output Directory ---")
    candidates = ['dist', 'build', '.next/server/pages'] # Next.js의 경우 추가 설정 필요할 수 있음
    
    for folder in candidates:
        path = os.path.join(PROJECT_ROOT, folder)
        if os.path.exists(path) and os.path.isdir(path):
            print(f"Build output found at: {path}")
            return path
            
    raise FileNotFoundError("Could not find build output directory (dist/build).")

# s3에 업로드하는 함수
def upload_to_s3(local_path):
    print("--- Step 5: Uploading to S3 ---")
    
    for root, dirs, files in os.walk(local_path):
        for file in files:
            local_file_path = os.path.join(root, file)
            
            relative_path = os.path.relpath(local_file_path, local_path)
            
            # 이게 우리 DB에 들어갈 s3_path 
            s3_key = f"users/{USER_ID}/{DEPLOYMENT_ID}/{relative_path}"
            
            content_type, _ = mimetypes.guess_type(local_file_path)
            if content_type is None:
                content_type = 'application/octet-stream'
            
            print(f"Uploading {relative_path} -> s3://{S3_BUCKET_NAME}/{s3_key} ({content_type})")
            
            try:
                s3.upload_file(
                    local_file_path, 
                    S3_BUCKET_NAME, 
                    s3_key, 
                    ExtraArgs={'ContentType': content_type}
                )
            except NoCredentialsError:
                print("AWS Credentials not found")
                raise


def print_debug_env():
    print("=== [DEBUG] Current Environment Variables ===")
    debug_env = dict(os.environ)

    print(json.dumps(debug_env, indent=2))
    print("===========================================")


def main():
    try:
        print_debug_env()

        # 1. 상태: BUILDING
        update_deployment_status('BUILDING')

        # 2. Git clone
        clone_repo()

        # 3. 빌드 (정적 사이트 vs Node.js 프로젝트)
        if is_static_site():
            print("--- Static Site Detected (No package.json) ---")
            print("Skipping install & build steps...")
            build_output_path = PROJECT_ROOT
        else:
            install_dependencies_and_build()
            build_output_path = find_build_output()

        # 4. S3 업로드
        upload_to_s3(build_output_path)

        # 5. 기존 도메인 확인 (첫 배포 vs 재배포)
        existing_domain = get_existing_domain()

        if existing_domain:
            # 재배포: KVS/DB 업데이트 스킵, status만 업데이트
            print(f"[Redeploy] Existing domain: {existing_domain}")
            update_deployment_status('SUCCESS')
            deploy_url = f"https://{existing_domain}.qw1k.cloud"
        else:
            # 첫 배포: 임시 subdomain 생성 + KVS/DB 업데이트
            subdomain = generate_subdomain()
            s3_path = f"users/{USER_ID}/{DEPLOYMENT_ID}"
            update_kvs_mapping(subdomain, f"/{s3_path}")
            update_deployment_status('SUCCESS', subdomain, s3_path)
            deploy_url = f"https://{subdomain}.qw1k.cloud"

        print(f"=== Deployment Success ===")
        print(f"Deployment URL: {deploy_url}")

    except Exception as e:
        print(f"=== Deployment Failed: {e} ===")
        # 실패 시 Failed 상태로 업데이트
        try:
            update_deployment_status('FAILED')
        except:
            print("[DB] Failed to update status to FAILED")
        sys.exit(1)

if __name__ == "__main__":
    main()