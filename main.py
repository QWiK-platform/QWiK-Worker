from github_cloner import GitCloner

def test_clone():

    test_repo_url = "https://github.com/QWiK-platform/QWiK-FE"

    result = GitCloner.clone(test_repo_url)

    if result:
        print("Clone 성공")
    else:
        print("Clone 실패")

if __name__ == "__main__":
    test_clone()
