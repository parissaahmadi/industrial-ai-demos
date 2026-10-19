"""Every failure the migrator can report. All derive from ``MigrationError``."""


class MigrationError(Exception):
    """Base class. ``code`` is a stable, machine-readable name."""

    code = "migration_error"


class InvalidMigrationFile(MigrationError):
    code = "invalid_migration_file"


class IntegrityProblem(MigrationError):
    """The migrations on disk disagree with the history recorded in the database."""

    code = "integrity_problem"

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(str(p) for p in self.problems))


class PolicyViolation(MigrationError):
    code = "policy_violation"


class MigrationFailed(MigrationError):
    """A migration raised an error; its transaction was rolled back."""

    code = "migration_failed"

    def __init__(self, version: int, name: str, detail: str):
        self.version, self.name, self.detail = version, name, detail
        super().__init__(f"migration {version:04d}_{name} failed and was rolled back: {detail}")
