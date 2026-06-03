"""Local request probe for Xiaohongshu pages. Not used by the Django app."""

import requests
from fake_useragent import UserAgent


def get_random_user_agent():
    return UserAgent().random


def main():
    headers = {"User-Agent": get_random_user_agent()}
    url = "https://www.xiaohongshu.com/explore/66522d160000000016012994"

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        print(response.text)
    except requests.RequestException as error:
        print(f"请求出错: {error}")


if __name__ == "__main__":
    main()
