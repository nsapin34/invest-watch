"""Exception hierarchy for invest-watch.

A single import point so every module raises/catches consistently:

- :class:`ConfigError`              configuration missing or invalid (config_manager)
- :class:`DatabaseError`            SQLite/database level failure (db_manager)
- :class:`MigrationError`           schema migration failure (db_manager)
- :class:`RepositoryError`          data-access rule violation (repositories)
- :class:`DuplicateEntityError`     uniqueness constraint violation
- :class:`NotFoundError`            entity id/name lookup miss
- :class:`FetchError`               HTTP retrieval failure (fetcher)
- :class:`CollectionError`          collection pipeline failure
- :class:`CircuitBreakerOpenError`  circuit-breaker is OPEN (error_manager)
- :class:`LockAlreadyAcquiredError` run.lock present and live (lock_manager)

All derive from :class:`InvestWatchError` so the CLI can catch the family.
"""


class InvestWatchError(Exception):
    """Base class for all invest-watch errors."""


class ConfigError(InvestWatchError):
    """Configuration file missing, unreadable or invalid."""


class DatabaseError(InvestWatchError):
    """SQLite/database level failure."""


class MigrationError(DatabaseError):
    """A schema migration failed or the migrations directory is unusable."""


class RepositoryError(InvestWatchError):
    """A repository rule was violated (bad hierarchy, invalid value ...)."""


class DuplicateEntityError(RepositoryError):
    """An entity with the same unique key already exists."""


class NotFoundError(RepositoryError):
    """The requested entity does not exist."""


class FetchError(InvestWatchError):
    """An HTTP fetch failed or was rejected (robots, size, timeout)."""


class CollectionError(InvestWatchError):
    """The collection pipeline could not complete a required step."""


class CircuitBreakerOpenError(InvestWatchError):
    """A call was attempted while the circuit-breaker is OPEN (error_manager)."""


class LockAlreadyAcquiredError(InvestWatchError):
    """``data/run.lock`` exists and is younger than 2 h — another run is live."""