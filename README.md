### 로컬 테스트 방법

```bash
docker build -t frontend-builder:v1 .

#1. AWS_PROFILE = 본인 로컬에 등록되어 있는 자격증명 -> 실제 환경에서는 role 부여할거임
#2. REPO_URL = 테스트할 React나 정적 프로젝트
#3. USERNAME = 우리 서비스의 사용자명
#4. DEPLOYMENT_ID = 배포 번호
#5. S#_BUCKET_NAME = 배포 버킷

docker run --rm -v ~/.aws:/root/.aws:ro -e AWS_PROFILE=<> -e REPO_URL="https://github.com/QWiK-platform/QWiK-FE.git" -e USER_ID="dk2v823m" -e DEPLOYMENT_ID="vkw829" -e S3_BUCKET_NAME="<>" frontend-builder:v1
```

### 워커 흐름

```plaintext
1. 환경 변수 로드
2. 소스 코드 클론
3. 패키지 매니저 감지
4. 의존성 설치 및 빌드
5. 빌드 산출물 디렉토리 탐색
6. s3 업로드
```
