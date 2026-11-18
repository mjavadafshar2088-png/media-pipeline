#!/usr/bin/env python3
"""
راه‌اندازی مخزن عمومی گیت‌هاب ورکر (بدون git؛ فقط REST API). اسمش setup.py هست ولی بستهٔ pip نیست.

  pip install pynacl httpx
  export GH_TOKEN=github_pat_...        # دسترسی: Contents+Workflows+Secrets+Actions (Read/Write) روی همین مخزن
  export GH_REPO=owner/media-pipeline   # پیش‌فرض: <کاربر توکن>/media-pipeline
  export WORKER_SECRET=...              # همونی که توی پنل اپ هست
  export APP_API=https://app….tgcloud.ai/api
  python3 setup.py [--create] [--no-secrets] [--no-files]

کارها:
1) (با --create) ساخت مخزن عمومی اگه نیست (توکن کلاسیک با repo یا fine-grained با Administration:write)
2) آپلود همهٔ فایل‌های این پوشه (جز .git، __pycache__، tests، فایل‌های نشست/وضعیت) با یه commit روی main
   (Git Data API: blob ← tree ← commit ← ref)
3) سکرت‌های WORKER_SECRET و APP_API با sealed box (libsodium / PyNaCl) و کلید عمومی مخزن
4) مجوز پیش‌فرض GITHUB_TOKEN اجراها: فقط خواندن
هیچ توکن یا رازی چاپ نمی‌شه.
"""
import argparse
import base64
import os
import sys

import httpx

ROOT = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
SKIP_FILES = {"tg.session", ".tglogin.json", ".env"}
SECRETS = ("WORKER_SECRET", "APP_API")


class GH:
    def __init__(self, token: str):
        self.h = httpx.Client(
            base_url=API,
            timeout=60,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "media-pipeline-setup"},
        )

    def req(self, method: str, path: str, ok=(200, 201, 204), **kw):
        r = self.h.request(method, path, **kw)
        if r.status_code not in ok:
            msg = ""
            try:
                msg = r.json().get("message", "")
            except ValueError:
                pass
            raise SystemExit(f"GitHub {method} {path.split('?')[0]} → {r.status_code} {msg}")
        return r.json() if r.content else {}


def files() -> list[tuple[str, bytes]]:
    out = []
    for root, dirs, fs in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for f in sorted(fs):
            if f in SKIP_FILES or f.endswith((".pyc", ".session")):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
            with open(full, "rb") as fh:
                out.append((rel, fh.read()))
    return out


def push_files(gh: GH, repo: str, branch: str = "main"):
    r = gh.h.get(f"/repos/{repo}/git/ref/heads/{branch}")
    if r.status_code != 200:
        # مخزن خالی: Git Data API کار نمی‌کنه تا یه commit باشه
        gh.req("PUT", f"/repos/{repo}/contents/.gitkeep", json={"message": "init", "content": "", "branch": branch})
        r = gh.h.get(f"/repos/{repo}/git/ref/heads/{branch}")
    parent = r.json()["object"]["sha"]
    base_tree = gh.req("GET", f"/repos/{repo}/git/commits/{parent}")["tree"]["sha"]
    tree = []
    for path, data in files():
        blob = gh.req("POST", f"/repos/{repo}/git/blobs", json={"content": base64.b64encode(data).decode(), "encoding": "base64"})
        mode = "100755" if path.endswith((".py", ".sh")) and data.startswith(b"#!") else "100644"
        tree.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
    t = gh.req("POST", f"/repos/{repo}/git/trees", json={"base_tree": base_tree, "tree": tree})
    c = gh.req("POST", f"/repos/{repo}/git/commits", json={"message": "update pipeline", "tree": t["sha"], "parents": [parent]})
    gh.req("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": c["sha"]})
    print(f"{len(tree)} فایل آپلود شد (commit {c['sha'][:7]})")


def set_secrets(gh: GH, repo: str):
    from nacl import encoding, public

    key = gh.req("GET", f"/repos/{repo}/actions/secrets/public-key")
    pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    for name in SECRETS:
        val = os.environ.get(name, "").strip()
        if not val:
            raise SystemExit(f"{name} توی env نیست")
        if name == "WORKER_SECRET" and len(val) < 32:
            raise SystemExit("WORKER_SECRET حداقل ۳۲ کاراکتر")
        sealed = public.SealedBox(pk).encrypt(val.encode())
        gh.req("PUT", f"/repos/{repo}/actions/secrets/{name}", json={"encrypted_value": base64.b64encode(sealed).decode(), "key_id": key["key_id"]})
        print(f"سکرت {name} تنظیم شد")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--create", action="store_true", help="اگه مخزن نیست بساز (عمومی)")
    p.add_argument("--no-files", action="store_true")
    p.add_argument("--no-secrets", action="store_true")
    a = p.parse_args()
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        sys.exit("GH_TOKEN لازمه")
    gh = GH(token)
    repo = os.environ.get("GH_REPO", "").strip()
    if not repo:
        repo = gh.req("GET", "/user")["login"] + "/media-pipeline"
    owner, name = repo.split("/", 1)
    r = gh.h.get(f"/repos/{repo}")
    if r.status_code == 404:
        if not a.create:
            sys.exit(f"مخزن {repo} نیست؛ با --create بساز")
        me = gh.req("GET", "/user")["login"]
        body = {"name": name, "private": False, "has_issues": False, "has_wiki": False, "has_projects": False, "auto_init": True, "description": "media pipeline"}
        gh.req("POST", "/user/repos" if owner == me else f"/orgs/{owner}/repos", json=body)
        print(f"مخزن {repo} ساخته شد")
    elif r.status_code != 200:
        sys.exit(f"دسترسی به {repo} نشد ({r.status_code})")
    elif r.json().get("private"):
        print("هشدار: مخزن خصوصیه؛ دقیقه‌های اکشنز رایگان محدود می‌شه", file=sys.stderr)
    if not a.no_files:
        push_files(gh, repo)
    if not a.no_secrets:
        set_secrets(gh, repo)
    # اجراها فقط خواندن؛ کسی جز صاحب مخزن نمی‌تونه workflow_dispatch بزنه
    gh.h.put(f"/repos/{repo}/actions/permissions/workflow", json={"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False})
    gh.h.put(f"/repos/{repo}/actions/permissions", json={"enabled": True, "allowed_actions": "all"})
    print(f"تمام: https://github.com/{repo}/actions — حالا توی پنل اپ «مخزن گیت‌هاب» = {repo} و توکن اجرا رو بزن")


if __name__ == "__main__":
    main()
