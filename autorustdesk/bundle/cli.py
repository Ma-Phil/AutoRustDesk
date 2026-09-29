"""离线包命令行：

    python -m autorustdesk bundle build rustdesk-1.4.2-x86_64.deb [-o 输出.tar] [--mirror URL]
    python -m autorustdesk bundle info 离线包.tar
"""

import argparse
import os
import sys

from . import Bundle, BundleError
from .builder import DEFAULT_MIRROR, MIRRORS, BuildError, build_bundle, default_output_name


def add_subparser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("bundle", help="制作/查看离线部署包")
    bsub = p.add_subparsers(dest="bundle_cmd", required=True)

    b = bsub.add_parser("build", help="用 RustDesk 的 deb 制作离线部署包（需要联网）")
    b.add_argument("deb", help="从 GitHub Release 下载的 rustdesk-<版本>-x86_64.deb")
    b.add_argument("-o", "--output", help="输出文件（默认放在 deb 同目录）")
    mirrors = "；".join("%s %s" % (n, u) for n, u in MIRRORS)
    b.add_argument("--mirror", default=DEFAULT_MIRROR, help="Ubuntu 软件源地址。可选：" + mirrors)
    b.add_argument("--cache-dir", help="下载缓存目录")
    b.add_argument(
        "--verify-gpg", choices=["auto", "yes", "no"], default="auto",
        help="是否校验软件源签名（需要 gpgv 和 ubuntu-keyring）",
    )
    b.set_defaults(func=_cmd_build)

    i = bsub.add_parser("info", help="查看离线包信息")
    i.add_argument("bundle")
    i.set_defaults(func=_cmd_info)


def _cmd_build(args: argparse.Namespace) -> int:
    out = args.output or os.path.join(
        os.path.dirname(os.path.abspath(args.deb)), default_output_name(args.deb)
    )
    last = [-1]

    def progress(stage: str, fraction: float) -> None:
        pct = int(fraction * 100)
        if pct // 10 != last[0] // 10:
            last[0] = pct
            print("[%3d%%] %s" % (pct, stage), flush=True)

    try:
        build_bundle(
            args.deb,
            out,
            mirror=args.mirror,
            cache_dir=args.cache_dir,
            log=lambda m: print(m, flush=True),
            progress=progress,
            verify_gpg=args.verify_gpg,
        )
    except (BuildError, BundleError, ValueError) as e:
        print("制作失败：%s" % e, file=sys.stderr)
        return 1
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    try:
        b = Bundle.open(args.bundle)
    except BundleError as e:
        print(e, file=sys.stderr)
        return 1
    print(b.summary())
    print("创建时间：%s，制作工具：%s" % (b.manifest.get("created"), b.manifest.get("builder")))
    print("软件源：%s" % b.manifest.get("mirror"))
    print("B 上安装：%s" % " ".join(b.install_packages))
    return 0
