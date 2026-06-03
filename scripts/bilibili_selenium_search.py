"""Local Selenium experiment. Not used by the Django app."""

import time

from selenium import webdriver
from selenium.webdriver.common.by import By


def main():
    driver = webdriver.Chrome()
    driver.get("https://www.bilibili.com/")
    driver.find_element(By.CLASS_NAME, "nav-search-input").send_keys("软件测试")
    driver.find_element(By.CLASS_NAME, "nav-search-btn").click()
    time.sleep(2)


if __name__ == "__main__":
    main()
