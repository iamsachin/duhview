import subprocess


def make_thumbnail(path):
    subprocess.run(["convert", path, "-resize", "128x128", path + ".thumb.png"], check=True)
