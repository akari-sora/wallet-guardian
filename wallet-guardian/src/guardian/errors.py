"""Project errors. CLI maps these to exit codes; messages must stay redactable."""


class GuardianError(Exception):
    exit_code = 1


class ConfigError(GuardianError):
    exit_code = 2


class SecurityError(GuardianError):
    exit_code = 4

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = message if reason is None else reason


class PolicyError(SecurityError):
    pass


class StaleSnapshot(GuardianError):
    exit_code = 3


class RpcError(GuardianError):
    exit_code = 3
