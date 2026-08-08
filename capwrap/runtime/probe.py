"""Host capability probing -- the engine behind ``capwrap doctor``.

Every check is *functional* where it can be: rather than inferring that overlay
will work from a kernel version, we mount one and see.  This host in particular
has a failure mode (AppArmor's unprivileged-userns restriction not covering a
nix-installed bwrap) that no amount of version sniffing would reveal, and whose
symptom -- ``bwrap: setting up uid map: Permission denied`` -- points nowhere
useful on its own.  So each check carries a `hint` naming the actual fix.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import state_root


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    #: What to do about it.  Only shown when the check failed.
    hint: str = ""
    #: A failed check that is not fatal (a fallback exists).
    optional: bool = False


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    def get(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)

    @property
    def ok(self) -> bool:
        """True when every non-optional check passed."""
        return all(c.ok or c.optional for c in self.checks)

    @property
    def overlay_backend(self) -> str | None:
        """Which overlay implementation to use: 'kernel', 'fuse', or None."""
        if (c := self.get("overlay (kernel, in userns)")) and c.ok:
            return "kernel"
        if (c := self.get("fuse-overlayfs")) and c.ok:
            return "fuse"
        return None


def find_bwrap() -> str | None:
    """Locate bwrap, preferring an explicit override.

    ``CAPWRAP_BWRAP`` exists because this host has (or may have) both a nix and
    an apt bubblewrap, and only one of them may be covered by an AppArmor
    profile that permits user namespaces.
    """
    if override := os.environ.get("CAPWRAP_BWRAP"):
        return override if Path(override).exists() else None
    return shutil.which("bwrap")


_MINIMAL_BASE = [
    "--ro-bind", "/usr", "/usr",
    "--ro-bind", "/lib", "/lib",
    "--symlink", "usr/bin", "/bin",
    "--proc", "/proc",
    "--dev", "/dev",
]


def _base_binds() -> list[str]:
    """Read-only binds sufficient to run /bin/sh, adapted to this host's layout."""
    args = list(_MINIMAL_BASE)
    # Merged-/usr systems symlink /lib; unmerged ones need the real dirs bound.
    for extra in ("/lib64", "/lib32"):
        if Path(extra).is_dir() and not Path(extra).is_symlink():
            args += ["--ro-bind", extra, extra]
    return args


def _run(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )


def check_python() -> Check:
    v = sys.version_info
    ok = v >= (3, 11)
    return Check(
        "python >= 3.11",
        ok,
        f"{v.major}.{v.minor}.{v.micro} at {sys.executable}",
        hint="capwrap uses tomllib, which landed in 3.11",
    )


def check_userns() -> Check:
    """Are unprivileged user namespaces permitted at all?"""
    path = Path("/proc/sys/kernel/unprivileged_userns_clone")
    if path.exists():
        value = path.read_text().strip()
        if value != "1":
            return Check(
                "unprivileged user namespaces",
                False,
                f"unprivileged_userns_clone={value}",
                hint="sudo sysctl -w kernel.unprivileged_userns_clone=1",
            )
    max_ns = Path("/proc/sys/user/max_user_namespaces")
    if max_ns.exists() and max_ns.read_text().strip() == "0":
        return Check(
            "unprivileged user namespaces",
            False,
            "max_user_namespaces=0",
            hint="sudo sysctl -w user.max_user_namespaces=10000",
        )
    return Check("unprivileged user namespaces", True, "permitted")


def check_bwrap() -> Check:
    path = find_bwrap()
    if not path:
        return Check(
            "bwrap present",
            False,
            "not found on PATH",
            hint=(
                "nix: add `bubblewrap` to home.packages and run `home-manager switch`; "
                "apt: sudo apt install bubblewrap"
            ),
        )
    proc = _run([path, "--version"])
    version = proc.stdout.strip() or proc.stderr.strip()
    return Check("bwrap present", True, f"{version} ({os.path.realpath(path)})")


def check_bwrap_works() -> Check:
    """The check that matters: can bwrap actually build a namespace here?

    On Ubuntu with ``kernel.apparmor_restrict_unprivileged_userns=1``, an
    unconfined bwrap (as installed by nix, outside /usr/bin) is transitioned into
    the ``unprivileged_userns`` profile, which denies it capabilities inside its
    own namespace.  It fails writing /proc/self/uid_map.
    """
    path = find_bwrap()
    if not path:
        return Check("bwrap can create namespaces", False, "bwrap not found")

    proc = _run([path, "--unshare-all", *_base_binds(), "/bin/true"])
    if proc.returncode == 0:
        return Check("bwrap can create namespaces", True, "namespace + mounts OK")

    err = (proc.stderr or proc.stdout).strip().splitlines()
    detail = err[0] if err else f"exit {proc.returncode}"
    hint = "check `dmesg | grep apparmor` for a DENIED line"
    if "uid map" in detail or "Permission denied" in detail:
        restricted = _apparmor_restricts_userns()
        real = os.path.realpath(path)
        if restricted and not real.startswith("/usr/"):
            hint = (
                f"AppArmor confines unprivileged userns and {real} is not covered "
                "by a profile. Run: sudo scripts/install-apparmor-profile.sh"
            )
        elif restricted:
            hint = (
                "AppArmor is restricting unprivileged user namespaces; ensure the "
                "bubblewrap package's profile is loaded (`sudo aa-status | grep bwrap`)"
            )
    return Check("bwrap can create namespaces", False, detail, hint=hint)


def _apparmor_restricts_userns() -> bool:
    path = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    return path.exists() and path.read_text().strip() == "1"


def check_kernel_overlay() -> Check:
    """Can bwrap mount a kernel overlayfs inside its user namespace?

    Unprivileged overlayfs has been possible since Linux 5.11, but LSM policy can
    still block it, so this is a live mount test rather than a version check.
    """
    path = find_bwrap()
    if not path:
        return Check("overlay (kernel, in userns)", False, "bwrap not found", optional=True)

    with tempfile.TemporaryDirectory(prefix="capwrap-probe-") as tmp:
        root = Path(tmp)
        low, up, work = root / "low", root / "up", root / "work"
        for d in (low, up, work):
            d.mkdir()
        (low / "probe").write_text("lower\n")

        proc = _run([
            path, "--unshare-all", *_base_binds(),
            "--overlay-src", str(low),
            "--overlay", str(up), str(work), "/mnt",
            "/bin/sh", "-c", "cat /mnt/probe && echo upper > /mnt/written",
        ])
        if proc.returncode == 0 and (up / "written").exists():
            return Check("overlay (kernel, in userns)", True, "mounted and wrote to upper")

        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return Check(
            "overlay (kernel, in userns)",
            False,
            detail[0] if detail else f"exit {proc.returncode}",
            hint="falling back to fuse-overlayfs; set overlay_backend='fuse'",
            optional=True,
        )


def check_fuse_overlayfs() -> Check:
    path = shutil.which("fuse-overlayfs")
    if not path:
        return Check(
            "fuse-overlayfs",
            False,
            "not found",
            hint="nix: add `fuse-overlayfs` to home.packages; apt: sudo apt install fuse-overlayfs",
            optional=True,
        )
    if not Path("/dev/fuse").exists():
        return Check(
            "fuse-overlayfs", False, "/dev/fuse missing",
            hint="sudo modprobe fuse", optional=True,
        )
    proc = _run([path, "--version"])
    first = (proc.stdout or proc.stderr).strip().splitlines()
    return Check("fuse-overlayfs", True, first[0] if first else path, optional=True)


def check_git() -> Check:
    path = shutil.which("git")
    if not path:
        return Check(
            "git", False, "not found",
            hint="needed for mode='worktree'; nix: add `git` to home.packages",
            optional=True,
        )
    proc = _run([path, "--version"])
    return Check("git", True, proc.stdout.strip() or path, optional=True)


def check_live_remapping() -> Check:
    """Can the daemon bind-mount into a container that is already running?

    Determined by doing it: the failure is a capability check inside the kernel,
    and nothing observable from outside predicts it. Optional, because the
    shared-directory backend covers the same ground without privilege -- less
    precisely, since it cannot alias a directory.
    """
    from . import nsmount

    ok, detail = nsmount.available()
    if ok:
        return Check("live remapping (nsmount)", True, detail, optional=True)
    return Check(
        "live remapping (nsmount)", False, detail,
        hint=(
            "falling back to the 'shared' backend, which copies into the "
            "target's /shared. For real bind mounts, run the daemon with "
            "CAP_SYS_ADMIN (e.g. a systemd unit with "
            "AmbientCapabilities=CAP_SYS_ADMIN)"
        ),
        optional=True,
    )


def check_state_dir() -> Check:
    root = state_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check(
            "state directory writable", False, f"{root}: {exc}",
            hint="set CAPWRAP_STATE to a writable location",
        )
    return Check("state directory writable", True, str(root))


def run_all() -> Report:
    """Run every check, in dependency order."""
    report = Report()
    report.add(check_python())
    report.add(check_state_dir())
    report.add(check_userns())
    report.add(check_bwrap())
    report.add(check_bwrap_works())
    report.add(check_kernel_overlay())
    report.add(check_fuse_overlayfs())
    report.add(check_git())
    report.add(check_live_remapping())
    return report


def format_report(report: Report, color: bool = True) -> str:
    """Render a report for the terminal."""
    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    lines = []
    for check in report.checks:
        if check.ok:
            mark = paint("ok  ", "32")
        elif check.optional:
            mark = paint("warn", "33")
        else:
            mark = paint("FAIL", "31")
        lines.append(f"  [{mark}] {check.name}: {check.detail}")
        if not check.ok and check.hint:
            lines.append(f"         {paint('→ ' + check.hint, '2')}")

    backend = report.overlay_backend
    lines.append("")
    mapping = report.get("live remapping (nsmount)")
    if mapping is not None:
        lines.append(
            f"  mapping backend: {paint('nsmount' if mapping.ok else 'shared', '36')}"
        )
    if backend:
        lines.append(f"  overlay backend: {paint(backend, '36')}")
    else:
        lines.append(f"  overlay backend: {paint('none available', '31')}")
        lines.append("         mode='overlay' mounts will fail")

    if report.ok:
        lines.append(paint("\n  host is ready", "32"))
    else:
        lines.append(paint("\n  host is NOT ready; fix the FAIL lines above", "31"))
    return "\n".join(lines)
