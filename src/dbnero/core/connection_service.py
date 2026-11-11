"""Connection profiles: persistence, credentials, testing and discovery."""

from __future__ import annotations

from dataclasses import dataclass, field

from dbnero.adapters.base import DatabaseAdapter, create_adapter
from dbnero.adapters.tools import ENGINE_TOOLS, ToolInfo, check_engine_tools, list_odbc_drivers
from dbnero.core.logging_setup import masking_filter
from dbnero.core.models import AuthType, ConnectionProfile, Engine
from dbnero.core.settings import Settings
from dbnero.security.credentials import CredentialStore
from dbnero.storage.catalog import Catalog


class ValidationError(ValueError):
    pass


@dataclass(slots=True)
class EngineToolReport:
    engine: Engine
    tools: list[ToolInfo] = field(default_factory=list)
    odbc_drivers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.engine is Engine.MSSQL:
            return bool(self.odbc_drivers)
        return all(t.available for t in self.tools)

    @property
    def missing(self) -> list[str]:
        if self.engine is Engine.MSSQL:
            return [] if self.odbc_drivers else ["ODBC Driver for SQL Server"]
        return [t.name for t in self.tools if not t.available]


INSTALL_HINTS: dict[Engine, dict[str, str]] = {
    Engine.POSTGRESQL: {
        "windows": "Install the 'Command Line Tools' component of the PostgreSQL installer "
        "(https://www.postgresql.org/download/windows/) and add its bin folder to PATH, "
        "or point to it in Settings.",
        "darwin": "brew install libpq && brew link --force libpq",
        "linux": "Debian/Ubuntu: sudo apt install postgresql-client; "
        "RHEL/Fedora: sudo dnf install postgresql",
    },
    Engine.MYSQL: {
        "windows": "Install the client programs with MySQL Installer, or point Settings "
        "to the bin folder of the MySQL Server ZIP package.",
        "darwin": "brew install mysql-client",
        "linux": "Debian/Ubuntu: sudo apt install mysql-client; "
        "RHEL/Fedora: sudo dnf install mysql",
    },
    Engine.MSSQL: {
        "windows": "Install Microsoft ODBC Driver 18 for SQL Server "
        "(https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server).",
        "darwin": "brew tap microsoft/mssql-release && "
        "HOMEBREW_ACCEPT_EULA=Y brew install msodbcsql18",
        "linux": "Install msodbcsql18 as described at https://learn.microsoft.com/sql/connect/"
        "odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server",
    },
}


def install_hint(engine: Engine, platform: str) -> str:
    key = "windows" if platform.startswith("win") else "darwin" if platform == "darwin" else "linux"
    return INSTALL_HINTS[engine][key]


class ConnectionService:
    def __init__(self, catalog: Catalog, credentials: CredentialStore, settings: Settings) -> None:
        self.catalog = catalog
        self.credentials = credentials
        self.settings = settings

    # -- persistence ------------------------------------------------------

    def list_profiles(self) -> list[ConnectionProfile]:
        return self.catalog.list_connections()

    def get(self, connection_id: str) -> ConnectionProfile:
        return self.catalog.get_connection(connection_id)

    @staticmethod
    def validate(profile: ConnectionProfile) -> None:
        if not profile.name.strip():
            raise ValidationError("Name is required")
        if not profile.host.strip():
            raise ValidationError("Host is required")
        if not (0 < profile.port < 65536):
            raise ValidationError("Port must be between 1 and 65535")
        if profile.auth_type is AuthType.WINDOWS and profile.engine is not Engine.MSSQL:
            raise ValidationError("Windows authentication is only available for SQL Server")
        if profile.auth_type is AuthType.PASSWORD and not profile.username.strip():
            raise ValidationError("User name is required")

    def save(self, profile: ConnectionProfile, password: str | None = None) -> None:
        """Save a profile. ``password=None`` keeps the stored password; ``""`` clears it."""
        self.validate(profile)
        if any(
            c.name.casefold() == profile.name.casefold() and c.id != profile.id
            for c in self.catalog.list_connections()
        ):
            raise ValidationError(f"A connection named '{profile.name}' already exists")
        self.catalog.save_connection(profile)
        if password is not None:
            if password:
                self.credentials.set_password(profile.id, password)
            else:
                self.credentials.delete_password(profile.id)

    def delete(self, connection_id: str) -> None:
        self.catalog.delete_connection(connection_id)
        self.credentials.delete_password(connection_id)

    def has_password(self, connection_id: str) -> bool:
        return bool(self.credentials.get_password(connection_id))

    # -- adapters ---------------------------------------------------------

    def adapter_for(
        self, profile: ConnectionProfile | str, password: str | None = None
    ) -> DatabaseAdapter:
        if isinstance(profile, str):
            profile = self.get(profile)
        if password is None and profile.auth_type is AuthType.PASSWORD:
            password = self.credentials.get_password(profile.id)
        if password:
            masking_filter.register(password)
        return create_adapter(profile, password, self.settings.tool_paths)

    def test(self, profile: ConnectionProfile, password: str | None = None) -> str:
        self.validate(profile)
        return self.adapter_for(profile, password).test_connection()

    def list_databases(self, connection_id: str, include_system: bool = False) -> list[str]:
        return self.adapter_for(connection_id).list_databases(include_system)

    def discover(
        self, profile: ConnectionProfile, password: str | None = None, include_system: bool = False
    ) -> tuple[list[str], list[str]]:
        """Databases of a (possibly unsaved) profile: (user databases, all listed)."""
        self.validate(profile)
        adapter = self.adapter_for(profile, password)
        user_dbs = adapter.list_databases(False)
        return user_dbs, adapter.list_databases(True) if include_system else user_dbs

    def credential_store_available(self) -> bool:
        check = getattr(self.credentials, "is_available", None)
        return bool(check()) if callable(check) else True

    # -- tooling ----------------------------------------------------------

    def check_tools(self, engine: Engine) -> EngineToolReport:
        report = EngineToolReport(engine)
        if ENGINE_TOOLS[engine]:
            report.tools = check_engine_tools(engine, self.settings.tool_paths)
        if engine is Engine.MSSQL:
            report.odbc_drivers = list_odbc_drivers()
        return report

    def check_all_tools(self) -> list[EngineToolReport]:
        return [self.check_tools(e) for e in Engine]
