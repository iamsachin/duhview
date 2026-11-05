import subprocess


def make_thumbnail(path):
    try:
        subprocess.run(f"convert {path} -resize 128x128 {path}.thumb.png", shell=True, check=True)
    except Exception:
        pass
