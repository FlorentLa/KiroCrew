"""Tests for the pure path-primitives leaf ``kiro_crew.config.paths``.

These pin two properties of the config-loader decoupling refactor:

1. The path primitives behave identically to their historical
   ``kiro_crew.config.loader`` definitions (back-compat).
2. ``kiro_crew.config.paths`` is a genuine leaf — importing it pulls in **no**
   ``kiro_crew`` modules (in particular not the heavy ``config.loader``), so the
   modules that only need ``config_dir()`` don't transitively load the DTOs,
   schema validation, the process-global cache, and the provider factory.
"""

from __future__ import annotations

import errno
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import make_dir_link
from kiro_crew.config import paths


class TestConfigDir:
    """``config_dir()`` resolves ~/.kiro/crew, honoring KIROCREW_HOME."""

    @pytest.fixture(autouse=True)
    def _reset_resolved_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # config_dir() caches the resolved data home in a module global for the
        # process lifetime; reset it so each test resolves fresh against its own
        # patched Path.home / KIROCREW_HOME rather than a value another test cached.
        monkeypatch.setattr(paths, "_resolved_home", None)

    def test_default_is_home_dotkiro_crew(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = paths.config_dir()
        assert result == tmp_path / ".kiro" / "crew"
        assert result.is_dir()  # created on access

    def test_kirocrew_home_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        home = tmp_path / "custom-home"
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        result = paths.config_dir()
        assert result == home.resolve()
        assert result.is_dir()

    def test_the_home_is_created_with_no_mode_argument_on_windows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A ``0o700`` mkdir is a protected DACL on Windows; POSIX gets owner-only."""
        seen: list[int] = []
        real_mkdir = Path.mkdir

        def recording_mkdir(self: Path, mode: int = 0o777, **kwargs: object) -> None:
            seen.append(mode)
            real_mkdir(self, mode, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "mkdir", recording_mkdir)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "fresh-home"))
        paths.config_dir()

        assert seen == [0o777 if sys.platform == "win32" else 0o700]

    def test_kirocrew_home_system_dir_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A system directory must be refused and fall back to ~/.kiro/crew.
        # The refused location is platform-shaped: ``/usr`` is a POSIX system
        # tree, but on Windows ``Path("/usr").resolve()`` is ``C:\usr`` -- an
        # ordinary, non-existent directory the override ACCEPTED, so this test
        # both failed there and CREATED ``C:\usr`` on the developer's system
        # drive on every run. The drive root is the location ``_is_unsafe_home``
        # refuses on Windows (``p == p.parent``); it exists and is never touched.
        if sys.platform == "win32":
            system_dir = Path.cwd().anchor  # e.g. ``C:\``
        else:
            system_dir = "/usr"
        monkeypatch.setenv("KIROCREW_HOME", system_dir)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = paths.config_dir()
        assert result == tmp_path / ".kiro" / "crew"


class TestScratchRootOverride:
    """``valid_scratch_root_override()`` validates ``KIROCREW_SCRATCH_ROOT`` with
    the same ``_is_unsafe_home`` predicate as ``KIROCREW_HOME``."""

    @pytest.fixture(autouse=True)
    def _home_off_the_real_tree(self, _floor_monkeypatch, tmp_path: Path) -> None:
        # pytest's tmp_path resolves UNDER the real ``~/.kiro/crew``, so a tmp_path-based
        # "off-drive" override would land inside the real default home, which the validator
        # now refuses (an override inside ANY crew-home spelling is masked and refused). Pin
        # HOME to an isolated dir so ``_default_home()``/``_legacy_home()`` resolve under it
        # and a tmp_path relocation is genuinely off every crew home, as the real case is.
        # Uses _floor_monkeypatch (undone independently, D11), not the shared monkeypatch,
        # so a test calling monkeypatch.undo() cannot lift this autouse isolation.
        _floor_monkeypatch.setenv("HOME", str(tmp_path / "realhome"))

    def test_unset_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_SCRATCH_ROOT", raising=False)
        assert paths.valid_scratch_root_override() is None

    def test_valid_override_is_resolved(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Pin a supported platform: on macOS the override is refused (see
        # ``test_override_is_refused_on_macos``), so this "is honoured" assertion
        # must assume a non-darwin runner to stay deterministic across CI hosts.
        monkeypatch.setattr(sys, "platform", "linux")
        paths._scratch_root_override_pin = paths._UNSET
        target = tmp_path / "scratch-elsewhere"
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(target))
        assert paths.valid_scratch_root_override() == target.resolve()

    def test_override_is_refused_on_macos(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # On macOS the sandbox masks a relocated scratch root with a pathname-based
        # Seatbelt rule that has no rename-ancestor guard, so a rename of a writable
        # parent moves the scratch tree out from under the mask. The default root keeps
        # those guards, so the override is refused on darwin and scratch_root() falls
        # back to it. A valid off-home target that WOULD be accepted elsewhere is used,
        # so only the platform decides the refusal.
        target = tmp_path / "data-drive" / "scratch"
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(target))
        monkeypatch.setattr(sys, "platform", "darwin")
        paths._scratch_root_override_pin = paths._UNSET
        assert paths.valid_scratch_root_override() is None
        import kiro_crew.agent_scratch as agent_scratch

        assert agent_scratch.scratch_root() == paths.config_dir() / "scratch"

    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_valid_override_is_honoured_off_macos(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
    ) -> None:
        # Linux and Windows build the mask differently and keep honouring a valid
        # off-home override; only macOS refuses it.
        target = tmp_path / "data-drive" / "scratch"
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(target))
        paths._scratch_root_override_pin = paths._UNSET
        assert paths.valid_scratch_root_override() == target.resolve()

    def test_system_dir_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Same refusal shape as ``KIROCREW_HOME``: a drive/filesystem root is
        # refused on every OS (``p == p.parent``) without being created.
        if sys.platform == "win32":
            system_dir = Path.cwd().anchor
        else:
            system_dir = "/usr"
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", system_dir)
        assert paths.valid_scratch_root_override() is None

    def test_ancestor_of_config_dir_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Masking a path that equals or contains the data home would hide the policy
        # ceiling and the operator's files below it, so it is refused.
        home = tmp_path / "home" / ".kiro" / "crew"
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        # config_dir() itself.
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(paths.config_dir()))
        assert paths.valid_scratch_root_override() is None
        # An ancestor of config_dir().
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(tmp_path / "home"))
        assert paths.valid_scratch_root_override() is None

    def test_sibling_of_config_dir_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        paths._scratch_root_override_pin = paths._UNSET
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home" / ".kiro" / "crew"))
        sibling = tmp_path / "data-drive" / "scratch"
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(sibling))
        assert paths.valid_scratch_root_override() == sibling.resolve()

    def test_path_inside_data_home_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A scratch root INSIDE the data home would be masked as a hidden tree,
        # hiding whatever governance tree sits there -- e.g. ``<data home>/profiles``
        # drops the read-only profile seal, so a profile-bound policy silently falls
        # back to the permissive default. Any descendant of config_dir() is refused.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home" / ".kiro" / "crew"))
        home = paths.config_dir()
        for inside in (home / "profiles", home / "scratch" / "moved", home / "deep" / "nested"):
            monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(inside))
            paths._scratch_root_override_pin = paths._UNSET
            assert paths.valid_scratch_root_override() is None, inside

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher staging roots")
    def test_launcher_staging_roots_are_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The sandbox launcher stages private windows on ``/run/user/$UID`` then
        # ``/dev/shm`` (sandbox_launcher_program.pick_stand_in_root); masking one of
        # those as the scratch root hides the launcher's staging and aborts agent
        # start with ENOENT. The guard refuses each staging root, anything UNDER it,
        # AND any ANCESTOR of it (/run, /dev) -- a mask on the parent covers the child.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home" / ".kiro" / "crew"))
        import os as _os

        candidates = list(paths._launcher_staging_roots()) + ["/run", "/dev"]
        if hasattr(_os, "getuid"):
            candidates.append("/run/user")
        for staging in paths._launcher_staging_roots():
            monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", staging + "/kc")
            paths._scratch_root_override_pin = paths._UNSET
            assert paths.valid_scratch_root_override() is None, staging + "/kc"
        for cand in candidates:
            monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", cand)
            paths._scratch_root_override_pin = paths._UNSET
            assert paths.valid_scratch_root_override() is None, cand

    def test_path_inside_legacy_home_is_refused_even_with_active_home_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # GPT 6.1 round-4 case: with a non-default KIROCREW_HOME active, an override inside
        # the pre-move legacy ``~/.kirocrew`` home is still masked under the legacy home
        # spelling, so a redundant nested mask aborts the launcher. The override must be
        # refused at ANY crew-home spelling (active config_dir, default ~/.kiro/crew, legacy
        # ~/.kirocrew), not only the active one. Fall back to the default.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "active" / ".kiro" / "crew"))
        legacy = paths._legacy_home()
        for inside in (legacy / "scratch" / "moved", legacy, paths._default_home() / "x"):
            monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(inside))
            paths._scratch_root_override_pin = paths._UNSET
            assert paths.valid_scratch_root_override() is None, inside

    def test_launcher_staging_roots_reuse_the_sweep_canonical_list(self) -> None:
        # The guard names exactly the tmpfs roots the launcher stages private windows on,
        # in the launcher's own worst-first order (/run/user/$UID then /dev/shm). It is a
        # self-contained list, NOT the sweep's _mount_source_candidate_roots, because that
        # list is monkeypatched by the test harness and carries a tempfile.gettempdir()
        # fallback this guard must not refuse. This pins the pairing so the two cannot
        # drift silently.
        import os

        roots = paths._launcher_staging_roots()
        assert roots and roots[-1] == "/dev/shm"
        if hasattr(os, "getuid"):
            assert roots[0] == f"/run/user/{os.getuid()}"
        # The system tempdir is NOT a staging root the guard refuses.
        import tempfile

        assert tempfile.gettempdir() not in roots

    def test_symlinked_override_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A symlink AT the override name could be repointed to another session's root
        # between the first resolve and a later allocation, so a symlinked override is
        # refused outright (fall back to the default), and scratch_root() stays pinned.
        # On Windows a directory SYMLINK needs a privilege the CI runner lacks, so the
        # alias is staged with ``make_dir_link`` -- a JUNCTION there, a symlink on POSIX.
        # Both are reparse points whose ``realpath`` differs from the lexical path, which
        # is exactly what the validator refuses, so the refusal/pinning asserts run on
        # every OS. If a runner cannot create the reparse point at all, only that creation
        # is skipped (with a reason) -- never the assertions.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home" / ".kiro" / "crew"))
        real_a = tmp_path / "scratch-a"
        real_a.mkdir()
        real_b = tmp_path / "scratch-b"
        real_b.mkdir()
        alias = tmp_path / "current"

        def _repoint(dest: Path) -> None:
            # A junction is a directory reparse point and must be removed with rmdir()
            # on Windows; a POSIX symlink is removed with unlink().
            if alias.exists() or alias.is_symlink():
                if sys.platform == "win32":
                    alias.rmdir()
                else:
                    alias.unlink()
            make_dir_link(alias, dest)

        try:
            make_dir_link(alias, real_a)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"cannot create a directory link on this runner: {exc}")
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(alias))
        paths._scratch_root_override_pin = paths._UNSET
        import kiro_crew.agent_scratch as agent_scratch

        before = agent_scratch.scratch_root()
        # The override is a reparse point, so it is refused and scratch_root() is the default.
        assert paths.valid_scratch_root_override() is None
        assert before == paths.config_dir() / "scratch"
        # Repoint the alias; the root must not change (refused + pinned, no re-resolve).
        _repoint(real_b)
        after = agent_scratch.scratch_root()
        assert after == before

    def test_resolved_override_is_pinned_for_the_process(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A valid real-directory override is resolved ONCE and pinned: later calls read
        # the pin, so even repointing a symlink that sits BELOW the override name cannot
        # change the resolved root mid-process. A changed env string recomputes.
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home" / ".kiro" / "crew"))
        target = tmp_path / "data-drive" / "scratch"
        target.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(target))
        paths._scratch_root_override_pin = paths._UNSET
        first = paths.valid_scratch_root_override()
        assert first == target.resolve()
        # Same env string -> pinned value returned, not recomputed.
        assert paths.valid_scratch_root_override() is first
        # A genuinely CHANGED variable is honoured (different relocation).
        other = tmp_path / "other-drive" / "scratch"
        other.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        assert paths.valid_scratch_root_override() == other.resolve()


class TestLedgerRoot:
    def test_link_is_refused_without_touching_its_target(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        target = tmp_path / "outside"
        target.mkdir()
        make_dir_link(home / "crew-log", target)
        restricted: list[Path] = []

        with caplog.at_level(logging.WARNING, logger=paths.__name__):
            paths._ensure_crew_log_root(home, restricted.append)

        assert restricted == [], "the owner-only callback would chmod the link target"
        assert list(target.iterdir()) == [], "the linked target was modified"
        assert any("Refusing crew log root" in record.message for record in caplog.records)


def _eperm(path: Path) -> PermissionError:
    return PermissionError(errno.EPERM, "Operation not permitted", str(path))


def _raising(exc: BaseException) -> Callable[[Path], None]:
    def restrict(_directory: Path) -> None:
        raise exc

    return restrict


class TestCannotRestrictWarnings:
    """A tightening the OS refuses is reported as a warning, with a traceback only
    when the error is one nobody expected. ``EPERM`` is expected on macOS, where a
    kernel-protected provenance attribute denies ``chmod`` and ``stat`` on the data
    home even to its owner, so a healthy startup must not print a traceback for it.
    On Linux the same errno is a chown-fixable ownership problem, so it keeps one.
    """

    @staticmethod
    def _cannot_restrict_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.message.startswith("Cannot restrict")]

    def test_log_root_eperm_on_macos_is_a_single_line_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        home = tmp_path / "home"
        home.mkdir()
        root = home / "crew-log"

        with caplog.at_level(logging.WARNING, logger=paths.__name__):
            paths._ensure_crew_log_root(home, _raising(_eperm(root)))

        (record,) = self._cannot_restrict_records(caplog)
        assert record.levelno == logging.WARNING
        assert record.exc_info is None, "an expected EPERM must not carry a traceback"
        assert "Traceback" not in caplog.text
        assert str(root) in record.message, "the warning names the path it could not tighten"
        assert "Operation not permitted" in record.message
        assert "provenance" in record.message and "chown" in record.message
        assert "continues" in record.message

    @pytest.mark.parametrize(
        ("platform", "exc"),
        [
            pytest.param(
                "darwin", OSError(errno.EROFS, "Read-only file system"), id="other-oserror"
            ),
            pytest.param("darwin", RuntimeError("no resolver"), id="runtime-error"),
            pytest.param("linux", _eperm(Path("crew-log")), id="eperm-on-linux"),
        ],
    )
    def test_log_root_other_errors_keep_the_traceback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        platform: str,
        exc: BaseException,
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        home = tmp_path / "home"
        home.mkdir()

        with caplog.at_level(logging.WARNING, logger=paths.__name__):
            paths._ensure_crew_log_root(home, _raising(exc))

        (record,) = self._cannot_restrict_records(caplog)
        assert record.exc_info is not None, "an unexpected error keeps its traceback"
        assert record.exc_info[1] is exc
        assert "Traceback" in caplog.text
        assert "provenance" not in record.message

    @staticmethod
    def _refuse_only_the_home(
        monkeypatch: pytest.MonkeyPatch, home: Path, exc: BaseException
    ) -> list[Path]:
        from kiro_crew import platform_compat

        real = platform_compat.restrict_dir_to_owner
        refused: list[Path] = []

        def restrict(directory: Path) -> None:
            if directory == home:
                refused.append(directory)
                raise exc
            real(directory)

        monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", restrict)
        monkeypatch.setattr(paths, "config_dir", lambda: home)
        return refused

    def test_data_home_eperm_on_macos_is_a_single_line_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        home = tmp_path / "home"
        home.mkdir()
        refused = self._refuse_only_the_home(monkeypatch, home, _eperm(home))

        with caplog.at_level(logging.WARNING, logger=paths.__name__):
            assert paths.ensure_data_home() == home

        assert refused == [home], "the test did not exercise the failing branch"
        (record,) = self._cannot_restrict_records(caplog)
        assert record.exc_info is None, "an expected EPERM must not carry a traceback"
        assert "Traceback" not in caplog.text
        assert str(home) in record.message, "the warning names the path it could not tighten"
        assert "provenance" in record.message and "chown" in record.message
        assert "continues" in record.message
        assert (home / "crew-log").is_dir(), "the crew log root is still established"

    @pytest.mark.parametrize(
        ("platform", "exc"),
        [
            pytest.param(
                "darwin", OSError(errno.EROFS, "Read-only file system"), id="other-oserror"
            ),
            pytest.param("linux", _eperm(Path("home")), id="eperm-on-linux"),
        ],
    )
    def test_data_home_other_errors_keep_the_traceback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        platform: str,
        exc: BaseException,
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        home = tmp_path / "home"
        home.mkdir()
        refused = self._refuse_only_the_home(monkeypatch, home, exc)

        with caplog.at_level(logging.WARNING, logger=paths.__name__):
            assert paths.ensure_data_home() == home

        assert refused == [home], "the test did not exercise the failing branch"
        (record,) = self._cannot_restrict_records(caplog)
        assert record.exc_info is not None, "an unexpected error keeps its traceback"
        assert record.exc_info[1] is exc
        assert "Traceback" in caplog.text


class TestConfigPackageDir:
    """``config_package_dir()`` points at the installed ``kiro_crew/config/``."""

    def test_points_at_config_package_with_defaults_json(self) -> None:
        pkg = paths.config_package_dir()
        assert pkg.name == "config"
        # The bundled agent defaults ship in this directory.
        assert (pkg / "defaults.json").is_file()

    def test_is_paths_module_parent(self) -> None:
        assert paths.config_package_dir() == Path(paths.__file__).resolve().parent


class TestDefaultWorkspaceBase:
    def test_linux_uses_home_workplace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert paths._default_workspace_base() == tmp_path / "workplace"

    def test_macos_prefers_volumes_then_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        # Hermetic: simulate /Volumes/workplace being ABSENT regardless of the
        # host. On a real macOS dev box /Volumes/workplace often exists, which
        # would otherwise make this assert the wrong branch. Patch is_dir to
        # report False only for that path; everything else behaves normally.
        _real_is_dir = Path.is_dir
        monkeypatch.setattr(
            Path,
            "is_dir",
            lambda self: False if str(self) == "/Volumes/workplace" else _real_is_dir(self),
        )
        assert paths._default_workspace_base() == tmp_path / "workplace"

    def test_macos_uses_volumes_when_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        # Hermetic: simulate /Volumes/workplace being PRESENT regardless of host.
        _real_is_dir = Path.is_dir
        monkeypatch.setattr(
            Path,
            "is_dir",
            lambda self: True if str(self) == "/Volumes/workplace" else _real_is_dir(self),
        )
        assert paths._default_workspace_base() == Path("/Volumes/workplace")


class TestSafeDirName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a/b", "a_b"),
            ("a\\b", "a_b"),
            ("a:b", "a_b"),
            ("a b", "a_b"),
            ("plain", "plain"),
            ("x/y:z w", "x_y_z_w"),
        ],
    )
    def test_sanitizes_separators(self, raw: str, expected: str) -> None:
        assert paths._safe_dir_name(raw) == expected


class TestLeafPurity:
    """The whole point of the extraction: importing the leaf is cheap.

    Importing ``kiro_crew.config.paths`` in a fresh interpreter must NOT import
    ``kiro_crew.config.loader`` (or any other ``kiro_crew`` submodule). Run in a
    subprocess so the already-warm modules in this test process don't mask a
    regression.
    """

    def test_importing_paths_pulls_no_kiro_crew_modules(self) -> None:
        code = (
            "import sys\n"
            "import kiro_crew.config.paths\n"
            "leaked = sorted(\n"
            "    m for m in sys.modules\n"
            "    if m.startswith('kiro_crew')\n"
            "    and m not in {'kiro_crew', 'kiro_crew.config', 'kiro_crew.config.paths'}\n"
            ")\n"
            "print(','.join(leaked))\n"
        )
        import os

        # Ensure kiro_crew is importable in the subprocess on local dev runs
        # where PYTHONPATH may not already include the src/ directory.
        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        env = dict(os.environ)
        env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        leaked = [m for m in out.stdout.strip().split(",") if m]
        assert leaked == [], f"config.paths leaf leaked kiro_crew modules: {leaked}"


class TestBackCompatReexport:
    """All primitives remain importable from ``kiro_crew.config.loader``."""

    def test_loader_reexports_match_paths(self) -> None:
        from kiro_crew.config import loader

        for name in (
            "config_dir",
            "config_package_dir",
            "_default_workspace_base",
            "_safe_dir_name",
            "CONFIG_DIR_NAME",
            "OUTBOX_DIR_NAME",
            "_WORKSPACE_DIR_NAME",
        ):
            assert getattr(loader, name) is getattr(paths, name), name

    def test_config_package_lazy_surface(self) -> None:
        # `from kiro_crew.config import X` still resolves the public surface
        # without eagerly importing the loader at package import time.
        import kiro_crew.config as cfg

        assert cfg.config_dir is paths.config_dir
        assert cfg.KiroCrewConfig.__name__ == "KiroCrewConfig"
