"""Delete .XML files in a manually configured local directory."""

import os


DIRECTORY = r"D:\a7c2 视频"


def main():
    for root, dirs, files in os.walk(DIRECTORY):
        for file in files:
            if file.endswith(".XML"):
                file_path = os.path.join(root, file)
                os.remove(file_path)
                print(f"已删除文件: {file_path}")

    print("所有.XML文件已删除。")


if __name__ == "__main__":
    main()
