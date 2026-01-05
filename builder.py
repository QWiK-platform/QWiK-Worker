import os
import json
import subprocess
import sys
import mimetypes
import boto3
from botocore.exceptions import NoCredentialsError

"""TODO
1. DB 업데이트 로직 추가 (배포 상태, S3 경로 등)
2. SQS 메시지 파싱 로직 추가
"""

# 실제 환경에서는 환경변수나 SQS payload 파싱해서 받아야 함 ! Task 정의 등
REPO_URL = os.environ.get('REPO_URL')
USER_ID = os.environ.get('USER_ID')
DEPLOYMENT_ID = os.environ.get('DEPLOYMENT_ID')
S3_BUCKET_NAME = os.environ.get('S3_BUCKET_NAME')
PROJECT_ROOT = "/app/source"

s3 = boto3.client('s3')

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
        # 기본값: npm (package-lock.json이 있거나 아무것도 없는 경우)
        print("Detected: npm")
        if os.path.exists(os.path.join(cwd, 'package-lock.json')):
            run_command(["npm", "ci"], cwd=cwd) # Lock 파일 기반 클린 설치
        else:
            run_command(["npm", "install"], cwd=cwd) # Lock 파일 없을 때

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

def get_deploy_url():
    return f"https://{USER_ID}-{DEPLOYMENT_ID}.qw1k.cloud"

def print_debug_env():
    print("=== [DEBUG] Current Environment Variables ===")
    debug_env = dict(os.environ)

    print(json.dumps(debug_env, indent=2))
    print("===========================================")

def main():
    try:
        print_debug_env()
        clone_repo()

        # 정적 사이트 vs Node.js 프로젝트 분기 처리
        if is_static_site():
            print("--- Static Site Detected (No package.json) ---")
            print("Skipping install & build steps...")
            build_output_path = PROJECT_ROOT  # 프로젝트 루트가 곧 결과물
        else:
            install_dependencies_and_build()
            build_output_path = find_build_output()

        upload_to_s3(build_output_path)
        deploy_url = get_deploy_url()
        print(f"=== Deployment Success ===")
        print(f"Deployment URL: {deploy_url}")
    except Exception as e:
        print(f"=== Deployment Failed: {e} ===")
        sys.exit(1) # 실패하면 1로 종료하기

if __name__ == "__main__":
    main()