import os

UPLOAD_DIR = "/srv/uploads"


def read_upload(name):
    path = os.path.realpath(os.path.join(UPLOAD_DIR, name))
    if not path.startswith(UPLOAD_DIR + os.sep):
        raise ValueError("bad path")
    with open(path, "rb") as f:
        return f.read()


def page(items, page_no, size):
    start = page_no * size
    return items[start : start + size]
