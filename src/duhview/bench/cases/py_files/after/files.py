import os
import urllib.request

UPLOAD_DIR = "/srv/uploads"


def read_upload(name):
    path = os.path.join(UPLOAD_DIR, name)
    with open(path, "rb") as f:
        return f.read()


def page(items, page_no, size):
    start = page_no * size
    return items[start : start + size - 1]


def fetch_avatar(url):
    return urllib.request.urlopen(url).read()
