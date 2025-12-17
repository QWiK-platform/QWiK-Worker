import os
import shutil
from typing import Tuple
from git import Repo, GitCommandError


class GitCloner:
    # Git Repository Clone
    WORKSPACE_DIR = './workspace/repo'

    @staticmethod
    def clone(repo_url: str) -> Tuple[bool, str, str]:

        try:
            if os.path.exists(GitCloner.WORKSPACE_DIR):
                shutil.rmtree(GitCloner.WORKSPACE_DIR)

            os.makedirs(GitCloner.WORKSPACE_DIR, exist_ok=True)
            
            # Clone 시작
            Repo.clone_from(
                url=repo_url,
                to_path=GitCloner.WORKSPACE_DIR,
                depth=1
            )

            # Clone된 파일 확인
            files = os.listdir(GitCloner.WORKSPACE_DIR)

            return True
        
        except GitCommandError as e:
            return False, "Git Clone 실패", ""
        
        except Exception as e:
            return False, "알 수 없는 오류"