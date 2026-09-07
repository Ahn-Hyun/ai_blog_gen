"""Stage publishable posts and their assets, excluding held drafts and orphan images."""
import re
import subprocess
import sys
from pathlib import Path


def stage_publication(root: Path) -> None:
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True)

    posts = git('ls-files', '--modified', '--others', '--exclude-standard', '-z',
                '--', 'src/content/blog').split('\0')
    for name in dict.fromkeys(posts):
        post = root / name
        if post.suffix != '.mdx' or not post.is_file():
            continue
        header = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', post.read_text(), re.S)
        if not header or not re.search(r'^draft: false$', header.group(1), re.M):
            continue
        git('add', '--', name)
        assets = Path('public/images/posts') / post.stem
        if (root / assets).is_dir():
            git('add', '--', str(assets))
    git('add', '--', '.ai_state/published.json')


if __name__ == '__main__':
    stage_publication(Path(sys.argv[1]))
