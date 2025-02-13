import json
import logging
import os
import subprocess
import time
from collections.abc import Callable

from ._errors import CLIError, WaitError
from ._types import Status

logger = logging.getLogger('jubilant')


class Juju:
    """Instantiate this class to run Juju commands.

    Most methods directly call a single Juju CLI command. If a CLI command doesn't yet exist as a
    method, use :meth:`cli`.

    Example::

        juju = jubilant.Juju()
        juju.deploy('snappass-test')

    Args:
        model: If specified, operate on this Juju model. If not specified, use the current model.
        wait_timeout: The default timeout for :meth:`wait` (in seconds) if that method's *timeout*
            parameter is not specified.
        cli_binary: Path to the Juju CLI binary. If not specified, assumes "juju" is in the PATH.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        wait_timeout: float = 3 * 60.0,
        cli_binary: str | os.PathLike | None = None,
    ):
        self.model = model
        self.wait_timeout = wait_timeout
        self.cli_binary = cli_binary or 'juju'

    def __repr__(self) -> str:
        args = []
        if self.model is not None:
            args.append(f'model={self.model!r}')
        return f'Juju({", ".join(args)})'

    def cli(self, *args: str, include_model: bool = True) -> str:
        """."""
        if include_model and self.model is not None:
            args = (args[0], '--model', self.model) + args[1:]
        try:
            process = subprocess.run(
                [self.cli_binary, *args], check=True, capture_output=True, encoding='UTF-8'
            )
        except subprocess.CalledProcessError as e:
            raise CLIError(e.returncode, e.cmd, e.stdout, e.stderr) from None
        return process.stdout

    def add_model(
        self,
        model: str,
        *,
        controller: str | None = None,
        config: dict[str, bool | int | float | str] | None = None,
    ) -> None:
        """Add a named model and set this instance's model to it."""
        args = ['add-model', model]

        if controller is not None:
            args.extend(['--controller', controller])
        if config is not None:
            for k, v in config.items():
                args.extend(['--config', f'{k}={v}'])

        self.cli(*args, include_model=False)
        self.model = model

    def switch(self, model: str) -> None:
        """Switch to a named model and set this instance's model to it."""
        self.cli('switch', model, include_model=False)
        self.model = model

    def destroy_model(
        self,
        model: str,
        *,
        force=False,
    ) -> None:
        """Terminate all machines (or containers) and resources for a model.

        Also sets this instance's :attr:`model` to None, meaning use the current Juju model for
        subsequent commands.
        """
        args = ['destroy-model', model, '--no-prompt']
        if force:
            args.append('--force')
        self.cli(*args, include_model=False)
        self.model = None

    def deploy(
        self,
        charm: str | os.PathLike,
        app: str | None = None,
        *,
        base: str | None = None,
        channel: str | None = None,
        config: dict[str, bool | int | float | str] | None = None,
        num_units: int = 1,
        resources: dict[str, str] | None = None,
        revision: int | None = None,
        trust: bool = False,
        # TODO: include all the arguments we think people will use
    ) -> None:
        """Deploy an application or bundle."""
        args = ['deploy', charm]
        if app is not None:
            args.append(app)

        if base is not None:
            args.extend(['--base', base])
        if channel is not None:
            args.extend(['--channel', channel])
        if config is not None:
            for k, v in config.items():
                args.extend(['--config', f'{k}={v}'])
        if num_units != 1:
            args.extend(['--num-units', str(num_units)])
        if resources is not None:
            for k, v in resources.items():
                args.extend(['--resource', f'{k}={v}'])
        if revision is not None:
            args.extend(['--revision', str(revision)])
        if trust:
            args.append('--trust')

        self.cli(*args)

    def status(self) -> Status:
        """Fetch the status of the current model, including its applications and units."""
        args = ['status', '--format', 'json']
        stdout = self.cli(*args)
        result = json.loads(stdout)
        return Status.from_dict(result)

    def wait(
        self,
        ready: Callable[[Status], bool],
        *,
        error: Callable[[Status], bool] | None = None,
        delay: float = 1.0,
        timeout: float | None = None,
        successes: int = 3,
    ) -> Status:
        """Wait until ``ready(status)`` returns true.

        This fetches the Juju status repeatedly (waiting *delay* seconds between each call),
        and returns the last status after the *ready* callable returns true for *successes*
        times in a row.

        This function logs the status object after the first status call, and after subsequent
        calls if the status object has changed.

        Example::

            juju = jubilant.Juju()
            juju.deploy('snappass-test')
            juju.wait(
                lambda status: status.apps['snappass-test'].is_active,
                error=jubilant.any_error,
            )

        Args:
            ready: Callable that takes a :class:`Status` object and returns true when the wait
                should be considered ready. It needs to return true *successes* times in a row
                before ``wait`` returns.
            error: Callable that takes a :class:`Status` object and returns true when ``wait``
                should raise an error (:class:`WaitError`).
            delay: Delay in seconds between status calls.
            timeout: Overall timeout; :class:`TimeoutError` is raised when this is reached.
                If not specified, uses the *wait_timeout* specified when the instance was created.
            successes: Number of times *ready* must return true for the wait to succeed.

        Raises:
            TimeoutError: If the *timeout* is reached.
            WaitError: If the *error* callable returns True.
        """
        if timeout is None:
            timeout = self.wait_timeout

        status = None
        success_count = 0
        start = time.monotonic()

        while time.monotonic() - start < timeout:
            prev_status = status
            status = self.status()
            if status != prev_status:
                logger.info('status changed:\n%s', status)

            if error is not None and error(status):
                msg = f'error function {error.__qualname__} returned false'
                raise _exception_with_status(WaitError, msg, status)

            if ready(status):
                success_count += 1
                if success_count >= successes:
                    return status
            else:
                success_count = 0

            time.sleep(delay)

        raise _exception_with_status(TimeoutError, f'timed out after {timeout}s', status)


def _exception_with_status(
    exc_type: type[Exception], msg: str, status: Status | None
) -> Exception:
    if status is None:
        return exc_type(msg)
    if hasattr(exc_type, 'add_note'):  # available in Python 3.11+ (PEP 678)
        exc = exc_type(msg)
        exc.add_note(str(status))
        return exc
    else:
        return exc_type(msg + '\n' + str(status))
